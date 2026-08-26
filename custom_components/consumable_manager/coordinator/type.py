"""耗材类型条目的协调器（ConsumableTypeCoordinator）。"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import re
from typing import Any, Callable, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.dt import as_local

from ..const import (
    CONF_LAST_REPLACED, CONF_ADDED_AT, CONF_BINDING_GROUPS, CONF_GROUP_ID,
    CONF_GROUP_KIND, CONF_GROUP_NAME, GROUP_KIND_CUSTOM, CONF_SOURCE_ENTITIES,
    CONF_ENTITY_REGEX, CONF_THRESHOLD, CONF_THRESHOLD_OPERATOR, CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT, DEFAULT_THRESHOLD, DEFAULT_THRESHOLD_TYPE, DEFAULT_THRESHOLD_UNIT,
    NOTIFY_STYLE_VALUE, NOTIFY_TEXT_CONSUMABLES, NOTIFY_TEXT_DESC_AREA, NOTIFY_TEXT_DESC_DEVICE,
    NOTIFY_TEXT_DESC_ENTITY, NOTIFY_TEXT_DESC_SPECS, NOTIFY_TEXT_LAST_REPLACED, NOTIFY_TEXT_REPLACE_NEEDED,
    NOTIFY_TEXT_UNKNOWN, OPERATOR_LESS_THAN, THRESHOLD_DEFAULT_OPERATOR, THRESHOLD_TYPE_REMAINING_TIME,
    THRESHOLD_TYPE_USED_TIME, TIME_UNIT_TO_HOURS, TODO_KIND_REPLACE, CONF_CONSUMABLE_ID,
    STATE_OK, STATE_REPLACE_NEEDED, TODO_STATUS_NEEDS_ACTION, TODO_STATUS_COMPLETED, TIME_UOM_TO_HOURS,
)

from ..library import Library, TypeMeta
from .base import (
    BaseCoordinator, TriggeredSet, _to_float, evaluate_threshold,
)
from .stock import _find_stock_coordinator

class ConsumableTypeCoordinator(BaseCoordinator):
    """耗材类型条目协调器：绑定实体 + 阈值提醒。"""
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        labels: dict[str, str] | None = None,
        type_meta: TypeMeta | None = None,
        library: Library | None = None,
    ) -> None:
        super().__init__(hass, entry, labels)
        # 该条目类型的库元数据（自定义类型不在库中时为 None）
        self._type_meta = type_meta
        # 合并库实例（内置 + 用户），用于待办描述展示耗材信息
        self._library = library
        # 刷新期自动重新加载合并库（仅由 build_coordinator 启用，见该函数）
        self._auto_reload_library = False

    def update_library(self, library: Library) -> None:
        """更新合并库引用（写库服务后调用，待办/通知立即用最新库匹配耗材）。"""
        self._library = library

    @property
    def cons_type(self) -> str:
        """耗材类型（电池 / 滤芯 / …）。"""
        return self.entry_type

    @property
    def entity_signature(self) -> tuple[str, ...]:
        """按分组生成诊断实体键（分组增删触发重载，重命名不触发漂移）。"""
        groups = self.groups
        if not groups:
            return ()
        return tuple(
            f"replace_status:{g.get(CONF_GROUP_ID, i)}"
            for i, g in enumerate(groups)
        )

    # ---- 绑定分组（多分组 → 多诊断实体；旧扁平 source_entities 向后兼容）----
    @property
    def groups(self) -> list[dict[str, Any]]:
        """本条目绑定分组列表（每个分组 = 一组源实体 + 可选阈值覆盖）。

        旧条目仅存扁平 CONF_SOURCE_ENTITIES 时，合成单「默认」分组
        （只读，不写回）；写操作一律经 config_flow / services 规范化到
        CONF_BINDING_GROUPS 后再落盘。
        """
        stored = self._entry.options.get(CONF_BINDING_GROUPS)
        if stored:
            return [dict(g) for g in stored]
        flat = list(self._entry.options.get(CONF_SOURCE_ENTITIES, []))
        if flat:
            return [
                {
                    CONF_GROUP_ID: "default",
                    CONF_GROUP_NAME: self.title or "默认",
                    CONF_SOURCE_ENTITIES: flat,
                }
            ]
        return []

    @property
    def source_snapshots(self) -> list[dict[str, Any]]:
        """全部分组的源实体快照并集（待办/订阅/通知/摘要统一读这份；含正则动态成员）。"""
        result: list[dict[str, Any]] = []
        for group in self.groups:
            if self._group_is_custom(group):
                continue
            manual = {
                s["entity_id"]: s
                for s in group.get(CONF_SOURCE_ENTITIES, [])
                if s.get("entity_id")
            }
            for eid in self._group_live_entities(group):
                if eid in manual:
                    result.append(manual[eid])
                else:
                    result.append(self._entity_snapshot(eid))
        return result

    def _entity_snapshot(self, entity_id: str) -> dict[str, Any]:
        """现场构建单个实体的最小快照（正则动态命中实体用；不依赖 config_flow）。"""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        reg_entry = None
        reg_get = getattr(ent_reg, "async_get", None)
        if callable(reg_get):
            reg_entry = reg_get(entity_id)
        device = None
        if reg_entry and getattr(reg_entry, "device_id", None):
            dev_get = getattr(dev_reg, "async_get", None)
            if callable(dev_get):
                device = dev_get(reg_entry.device_id)
        return {
            "entity_id": entity_id,
            "device_name": getattr(device, "name", None),
            "device_model": getattr(device, "model", None),
            "manufacturer": getattr(device, "manufacturer", None),
        }

    def _regex_match_entities(self, pattern: str) -> list[str]:
        """按正则规则运行时匹配当前实体注册表中的实体 ID。

        与手动多选取并集构成分组 live 成员；新增匹配实体在下次刷新自动入组。
        仅匹配 sensor. 域且未被禁用的实体（与手动 EntitySelector 域一致）。
        """
        pattern = (pattern or "").strip()
        if not pattern:
            return []
        try:
            compiled = re.compile(pattern)
        except re.error:
            return []
        ent_reg = er.async_get(self.hass)
        entities = getattr(ent_reg, "entities", None) or {}
        return [
            entity_id
            for entity_id, entry in entities.items()
            if (
                not getattr(entry, "disabled_by", None)
                and entity_id.startswith("sensor.")
                and compiled.search(entity_id)
            )
        ]

    def _group_live_entities(self, group: dict[str, Any]) -> list[str]:
        """分组的 live 成员 = 手动固定快照 ∪ 正则规则运行时匹配（去重保序）。"""
        if self._group_is_custom(group):
            return []
        manual = [
            s["entity_id"]
            for s in group.get(CONF_SOURCE_ENTITIES, [])
            if s.get("entity_id")
        ]
        regex = self._regex_match_entities(group.get(CONF_ENTITY_REGEX, ""))
        return sorted(set(manual) | set(regex))

    def _group_manual_entities(self, group: dict[str, Any]) -> list[str]:
        """分组内手动固定实体（不含正则动态匹配）。"""
        return [
            s["entity_id"]
            for s in group.get(CONF_SOURCE_ENTITIES, [])
            if s.get("entity_id")
        ]

    def _group_entities(self, group: dict[str, Any]) -> list[str]:
        """分组内实体（live：手动固定 ∪ 正则动态匹配；群组递归展开为叶子）。"""
        return self._group_live_entities(group)

    def _group_resolved(self, group: dict[str, Any]) -> list[str]:
        """分组内实体（群组递归展开为叶子）。"""
        return self._expand_groups(self._group_entities(group))

    @property
    def source_entities(self) -> list[str]:
        return [
            snapshot["entity_id"]
            for snapshot in self.source_snapshots
            if snapshot.get("entity_id")
        ]

    def _expand_groups(self,
        entity_ids: list[str],
        seen: set[str] | None = None,
    ) -> list[str]:
        """递归展开群组成员（防循环去重保序；每次重解析 = 动态跟随）。"""
        seen = seen or set()
        result: list[str] = []
        for entity_id in entity_ids:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            state = self.hass.states.get(entity_id)
            members: list[str] | None = None
            if state is not None:
                attr = state.attributes.get("entity_id")
                is_group = entity_id.startswith("group.") or (
                    isinstance(attr, list)
                    and bool(attr)
                    and all(
                        isinstance(m, str) and "." in m for m in attr
                    )
                )
                if is_group and isinstance(attr, list):
                    members = attr
            if members:
                result.extend(self._expand_groups(members, seen))
            else:
                result.append(entity_id)
        return result

    def resolved_source_entities(self) -> list[str]:
        """解析后的实际实体 ID 列表（群组已层层展开为叶子实体）。"""
        if self._resolved_entities is None:
            self._resolved_entities = self._expand_groups(self.source_entities)
        return list(self._resolved_entities)

    def _read_values(
        self, entity_ids: list[str], threshold_type: str | None = None
    ) -> list[float | None]:
        """读取指定实体的实时值（缺失 / 不可用返回 None）。"""
        convert = (threshold_type or self.threshold_type) in (
            THRESHOLD_TYPE_REMAINING_TIME, THRESHOLD_TYPE_USED_TIME
        )
        values: list[float | None] = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable", None):
                values.append(None)
                continue
            raw = _to_float(state.state)
            if raw is None:
                values.append(None)
                continue
            if convert:
                uom = state.attributes.get("unit_of_measurement")
                factor = TIME_UOM_TO_HOURS.get(str(uom)) if uom else None
                if factor is not None:
                    raw = raw * factor
            values.append(raw)
        return values

    def bound_values(self) -> list[float | None]:
        """全部分组展开后实体的实时值并集（待办/通知显示用）。"""
        return self._read_values(self.resolved_source_entities())

    def triggered_entities(self) -> list[str]:
        """当前越过阈值的实际实体。对外统一读单一事实源；未刷新 fallback。"""
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "replace"
        ):
            return list(self._current_triggered.members)
        return self._eval_triggered_entities()

    def triggered_values(self) -> list[float | None]:
        """越过阈值的实体对应值（与 triggered_entities 同序）。"""
        resolved_ids = list(self.resolved_source_entities())
        all_values = self.bound_values()
        id_to_value = dict(zip(resolved_ids, all_values))
        triggered = self.triggered_entities()
        return [id_to_value.get(eid) for eid in triggered]

    # ---- 阈值（兜底链：options → 类型元数据 → 通用兜底值）----
    @property
    def threshold_type(self) -> str:
        value = self._entry.options.get(CONF_THRESHOLD_TYPE)
        if value:
            return value
        if self._type_meta is not None:
            return self._type_meta.default_threshold_type
        return DEFAULT_THRESHOLD_TYPE

    @property
    def threshold(self) -> float | None:
        value = self._entry.options.get(CONF_THRESHOLD)
        if value is not None:
            return _to_float(value)
        if self._type_meta is not None:
            return self._type_meta.default_threshold
        return DEFAULT_THRESHOLD

    @property
    def threshold_unit(self) -> str:
        value = self._entry.options.get(CONF_THRESHOLD_UNIT)
        if value:
            return value
        if self._type_meta is not None:
            return self._type_meta.default_threshold_unit
        return DEFAULT_THRESHOLD_UNIT

    @property
    def threshold_operator(self) -> str:
        return self._entry.options.get(
            CONF_THRESHOLD_OPERATOR,
            THRESHOLD_DEFAULT_OPERATOR.get(
                self.threshold_type, OPERATOR_LESS_THAN
            ),
        )

    @property
    def _trigger_kind(self) -> Literal["replace", "low_stock"]:
        return "replace"

    def _group_threshold(
        self, group: dict[str, Any]
    ) -> tuple[str, float | None, str, str]:
        """分组阈值：分组覆盖优先，否则回退条目级（再回退类型/通用在属性里）。

        返回 (type, value, unit, operator)。
        """
        override = group.get(CONF_THRESHOLD)
        if override is not None:
            return (
                group.get(CONF_THRESHOLD_TYPE) or self.threshold_type,
                _to_float(override),
                group.get(CONF_THRESHOLD_UNIT) or self.threshold_unit,
                group.get(CONF_THRESHOLD_OPERATOR) or self.threshold_operator,
            )
        return (
            self.threshold_type,
            self.threshold,
            self.threshold_unit,
            self.threshold_operator,
        )

    def _compute_triggered(self) -> TriggeredSet:
        """更换触发集合评估：评估阶段唯一入口。"""
        self._resolved_entities = self._expand_groups(self.source_entities)
        return TriggeredSet(
            kind="replace",
            members=tuple(self._eval_triggered_entities()),
        )

    def _eval_triggered_entities(self) -> list[str]:
        """触发实体评估（评估阶段唯一调用；下游只读 _current_triggered）。"""
        triggered: list[str] = []
        for group in self.groups:
            g_resolved = self._group_resolved(group)
            ttype, tval, tunit, top = self._group_threshold(group)
            for entity_id, value in zip(
                g_resolved, self._read_values(g_resolved, ttype)
            ):
                if evaluate_threshold(ttype, tval, top, [value], tunit):
                    triggered.append(entity_id)
        return triggered

    @property
    def replace_status(self) -> str:
        """更换状态：直接从 TriggeredSet 取整体状态。"""
        if self._current_triggered is None:
            return (
                STATE_REPLACE_NEEDED
                if self._eval_triggered_entities()
                else STATE_OK
            )
        if self._current_triggered.kind == "replace":
            return self._current_triggered.overall_state()
        return (
            STATE_REPLACE_NEEDED
            if self._eval_triggered_entities()
            else STATE_OK
        )

    @property
    def last_replaced(self) -> str | None:
        return self._entry.options.get(CONF_LAST_REPLACED)

    # ---- 自定义耗材实体分组（无绑定实体，按 added_at 计时）----
    def _group_is_custom(self, group: dict[str, Any]) -> bool:
        """该分组是否为自定义耗材实体（自建数据，不绑定实体）。"""
        return group.get(CONF_GROUP_KIND) == GROUP_KIND_CUSTOM

    def _custom_added_at(self, group: dict[str, Any]) -> datetime | None:
        """解析添加/更换时间（ISO 日期或日期时间），无/非法返回 None。"""
        raw = group.get(CONF_ADDED_AT)
        if not raw:
            return None
        try:
            added = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if isinstance(added, date) and not isinstance(added, datetime):
            added = datetime(added.year, added.month, added.day)
        if added.tzinfo is None:
            added = added.replace(tzinfo=timezone.utc)
        return added

    def custom_elapsed_hours(self, group: dict[str, Any]) -> float | None:
        """已使用时长（小时）；未配置 added_at 返回 None。"""
        added = self._custom_added_at(group)
        if added is None:
            return None
        now = datetime.now(timezone.utc)
        return (now - added).total_seconds() / 3600.0

    def _custom_triggered(self, group: dict[str, Any]) -> bool:
        """自定义分组触发判定：已使用时长 > 阈值（used_time + greater_than）。"""
        elapsed = self.custom_elapsed_hours(group)
        if elapsed is None:
            return False
        _ttype, tval, tunit, top = self._group_threshold(group)
        # 自定义分组恒按「已使用时长」语义评估（与表单固定阈值类型一致）
        return evaluate_threshold(
            THRESHOLD_TYPE_USED_TIME, tval, top, [elapsed], tunit
        )

    def group_status(self, group: dict[str, Any]) -> str:
        """分组更换状态：分组内任一实体触发即「需要更换」。"""
        if self._group_is_custom(group):
            return STATE_REPLACE_NEEDED if self._custom_triggered(group) else STATE_OK
        g_resolved = set(self._group_resolved(group))
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "replace"
        ):
            triggered = set(self._current_triggered.members)
        else:
            triggered = set(self._eval_triggered_entities())
        return (
            STATE_REPLACE_NEEDED
            if g_resolved & triggered
            else STATE_OK
        )

    def group_attributes(self, group: dict[str, Any]) -> dict[str, Any]:
        """分组诊断实体属性：名称/阈值/实体/触发列表。"""
        if self._group_is_custom(group):
            ttype, tval, tunit, top = self._group_threshold(group)
            elapsed = self.custom_elapsed_hours(group)
            # 已用时长换算到阈值单位（与 evaluate_threshold 口径一致），便于对照
            factor = TIME_UNIT_TO_HOURS.get(tunit, 1.0)
            elapsed_in_unit = (elapsed / factor) if elapsed is not None else None
            cid = group.get(CONF_CONSUMABLE_ID)
            bound = self._library.get(cid) if cid else None
            return {
                "group": group.get(CONF_GROUP_NAME),
                "custom_consumable_entity": True,
                "consumable_type": self.cons_type,
                "binding_consumable": (
                    bound.display_name(self.hass.config.language) if bound
                    else None
                ),
                "added_at": group.get(CONF_ADDED_AT),
                "elapsed": elapsed_in_unit,
                "threshold_type": THRESHOLD_TYPE_USED_TIME,
                "threshold": tval,
                "threshold_unit": tunit,
                "threshold_operator": top,
                "last_replaced": self.last_replaced,
            }
        g_resolved = set(self._group_resolved(group))
        ttype, tval, tunit, top = self._group_threshold(group)
        manual = self._group_manual_entities(group)
        regex = self._regex_match_entities(group.get(CONF_ENTITY_REGEX, ""))
        return {
            "group": group.get(CONF_GROUP_NAME),
            "consumable_type": self.cons_type,
            "threshold_type": ttype,
            "threshold": tval,
            "threshold_unit": tunit,
            "threshold_operator": top,
            "source_entities": self._group_entities(group),
            "manual_entities": manual,
            "regex_matched": regex,
            "triggered_entities": [
                e for e in self.triggered_entities() if e in g_resolved
            ],
            "last_replaced": self.last_replaced,
        }

    def status_attributes(self) -> dict[str, Any]:
        return {
            "consumable_type": self.cons_type,
            "threshold_type": self.threshold_type,
            "threshold": self.threshold,
            "threshold_unit": self.threshold_unit,
            "threshold_operator": self.threshold_operator,
            "source_entities": self.source_entities,
            "triggered_entities": self.triggered_entities(),
            "last_replaced": self.last_replaced,
        }

    # ---- 写入 ----
    def _last_replaced_label(self) -> str | None:
        """「上次更换时间」可读文案（本地时区），无记录返回 None。"""
        raw = self.last_replaced
        if not raw:
            return None
        try:
            local = as_local(datetime.fromisoformat(raw))
        except ValueError:
            return raw
        return self._md_kv(
            NOTIFY_TEXT_LAST_REPLACED,
            local.strftime("%Y-%m-%d %H:%M"),
        )

    def _custom_bound_consumables_label(self) -> str | None:
        """自定义分组绑定了具体耗材时，待办优先显示该耗材（而非类型全部）。"""
        library = self._library
        if library is None:
            return None
        bound: list = []
        for group in self.groups:
            if not self._group_is_custom(group):
                continue
            cid = group.get(CONF_CONSUMABLE_ID)
            if cid:
                c = library.get(cid)
                if c is not None:
                    bound.append(c)
        if not bound:
            return None
        locale = self.hass.config.language
        names = "、".join(
            f"{c.display_name(locale)}（{c.unit}）" for c in bound
        )
        return self._md_kv(NOTIFY_TEXT_CONSUMABLES, names)

    def _entity_area(self, entity_id: str) -> str | None:
        """实体所属区域名（实体 → 设备 → 区域注册表），无则 None。"""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        ent = ent_reg.async_get(entity_id)
        if ent is None or not getattr(ent, "device_id", None):
            return None
        device = dev_reg.async_get(ent.device_id)
        if device is None or not getattr(device, "area_id", None):
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return getattr(area, "name", None) or None

    def _device_mm(self, entity_id: str) -> tuple[str | None, str | None]:
        """从实体→设备注册表取厂商+型号（实时，避免固化快照过期）。

        与 services.py 的 _resolve_entity_info 同源；query_bindings 的
        suggested 走的就是这份实时设备信息，保证待办描述口径一致。
        """
        ent_reg = er.async_get(self.hass)
        reg_get = getattr(ent_reg, "async_get", None)
        if not callable(reg_get):
            return None, None
        ent = reg_get(entity_id)
        if ent is None or not getattr(ent, "device_id", None):
            return None, None
        dev_reg = dr.async_get(self.hass)
        dev_get = getattr(dev_reg, "async_get", None)
        if not callable(dev_get):
            return None, None
        device = dev_get(ent.device_id)
        if device is None:
            return None, None
        return (
            getattr(device, "manufacturer", None),
            getattr(device, "model", None),
        )

    def _entity_consumables(self,
        snapshot: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """设备绑定的耗材（库内按 manufacturer+model 匹配）。"""
        if self._library is None:
            return None, None
        # 解析设备厂商/型号：优先实时注册表（实体→设备），与 query_bindings 的
        # suggested 口径完全一致；注册表取不到（如手动绑定且无设备实体）时回退快照。
        # 这样即使固化快照缺/过期，只要实体仍挂在 HA 设备上，就能解析出具体耗材，
        # 而非错误地回退「类型全部耗材」或直接「未知」。
        manufacturer = snapshot.get("manufacturer")
        model = snapshot.get("device_model")
        if (eid := snapshot.get("entity_id")):
            live_m, live_model = self._device_mm(eid)
            if live_m or live_model:
                manufacturer = live_m or manufacturer
                model = live_model or model
        items = self._library.find_compatible(manufacturer, model)
        if not items:
            # 未匹配到具体耗材（设备未收录 / 无设备信息）→ 交由调用方显示「未知」，
            # 不再回退「类型全部耗材」，避免误把整类耗材（如全部电池）列出来。
            return None, None
        locale = self.hass.config.language
        names = "、".join(
            f"{c.display_name(locale)}（{c.unit}）" for c in items
        )
        # 规格仅给 JSON（名称已在「耗材」行展示，避免重复前缀），多条用分号拼接为单行
        specs = "；".join(
            json.dumps(c.meta, ensure_ascii=False)
            for c in items if c.meta
        )
        return names, specs or None

    def _replace_summary(self, entity_id: str) -> str:
        """更换待办标题：设备名称 + 「请更换耗材。」（通用话术，按语言）。"""
        snapshots = {
            snap["entity_id"]: snap
            for snap in self.source_snapshots
            if snap.get("entity_id")
        }
        snapshot = snapshots.get(entity_id, {})
        state = self.hass.states.get(entity_id)
        state_name = (
            getattr(state, "name", None)
            or (state.attributes.get("friendly_name") if state else None)
        )
        # 显示名取值：friendly_name 优先（用户自定义实体名），
        # 其次设备注册表设备名，再次型号，最后实体 id。
        display = (
            state_name
            or snapshot.get("device_name")
            or snapshot.get("device_model")
            or entity_id
        )
        return f"{display} {self._notify_text(NOTIFY_TEXT_REPLACE_NEEDED)}"

    def _replace_description(self, entity_id: str | None = None) -> str | None:
        """更换待办描述（单实体）：区域/设备/实体/耗材/规格；未绑定耗材显示"""
        parts: list[str] = []
        snapshots = {
            snap["entity_id"]: snap
            for snap in self.source_snapshots
            if snap.get("entity_id")
        }
        for eid in ([entity_id] if entity_id else self.triggered_entities()):
            lines: list[str] = []
            if area := self._entity_area(eid):
                lines.append(self._md_kv(NOTIFY_TEXT_DESC_AREA, area))
            state = self.hass.states.get(eid)
            state_name = (
                getattr(state, "name", None)
                or (state.attributes.get("friendly_name") if state else None)
            )
            # 正则/手动匹配的实体没有绑定快照（不在 source_snapshots 中），
            # 但仍可能关联了 HA 设备。传 entity_id 让 _entity_consumables 的
            # 实时注册表回退能反查设备→耗材，与 query_bindings 的 suggested 口径一致；
            # 若实体确实无任何设备信息，才在调用方显示「未知」。
            snapshot = snapshots.get(eid) or {"entity_id": eid}
            display = state_name or snapshot.get("device_name")
            if display:
                lines.append(self._md_kv(NOTIFY_TEXT_DESC_DEVICE, display))
            lines.append(self._md_kv(NOTIFY_TEXT_DESC_ENTITY, eid))
            cons_names, specs = self._entity_consumables(snapshot)
            if cons_names:
                lines.append(self._md_kv(NOTIFY_TEXT_CONSUMABLES, cons_names))
            else:
                lines.append(
                    self._md_kv(
                        NOTIFY_TEXT_CONSUMABLES,
                        self._notify_text(NOTIFY_TEXT_UNKNOWN),
                    )
                )
            if specs:
                # 规格用 Markdown 行内代码展示（已省略名称前缀），多条用分号分隔
                lines.append(
                    self._md_kv(NOTIFY_TEXT_DESC_SPECS, f"`{specs}`")
                )
            parts.append("\n".join(lines))
        if not parts:
            # 无触发实体（如刚恢复）：优先自定义分组绑定的具体耗材；
            # 其次按本条目各绑定实体的设备（find_compatible）匹配具体耗材，
            # 命中则显示具体耗材而非类型全部；全未命中显示「未知」。
            label = self._custom_bound_consumables_label()
            if label is None:
                matched: list[str] = []
                seen: set[str] = set()
                for snap in self.source_snapshots:
                    names, _specs = self._entity_consumables(snap)
                    if names and names not in seen:
                        matched.append(names)
                        seen.add(names)
                if matched:
                    label = self._md_kv(
                        NOTIFY_TEXT_CONSUMABLES, "、".join(matched))
                else:
                    label = self._md_kv(
                        NOTIFY_TEXT_CONSUMABLES,
                        self._notify_text(NOTIFY_TEXT_UNKNOWN),
                    )
            parts.append(label)
        if last := self._last_replaced_label():
            parts.append(last)
        return "\n\n".join(parts) if parts else None

    @callback
    def async_mark_replaced(self, uid: str | None = None) -> None:
        """标记已更换：记录时间、完成「更换」待办，并联动扣减关联类型的库存项。"""
        options = self.options
        options[CONF_LAST_REPLACED] = datetime.now(timezone.utc).isoformat()
        self._write_options(options)
        prefix = self._auto_uid(TODO_KIND_REPLACE)
        if uid:
            self._complete_todo(uid)
            # 勾选瞬间把描述同步为刚发生的时间（下次刷新也会刷新）
            if uid in self._todos:
                entity_id = uid[len(prefix) + 1:]
                self._todos[uid]["description"] = (
                    self._replace_description(entity_id)
                )
        else:
            for other in list(self._todos):
                if other == prefix or other.startswith(prefix + "_"):
                    self._complete_todo(other)
        # 联动扣减：库存条目中与该耗材类型绑定的库存项各 -1
        stock = _find_stock_coordinator(self.hass)
        if stock is not None:
            for item_id in stock.items_for_type(self.cons_type):
                stock.async_add_quantity(item_id, -1)

    @callback
    def async_on_todo_completed(
        self, uid: str, old_status: str | None, new_status: str
    ) -> None:
        """勾选「更换」待办 = 已更换（仅自动更换待办、needs_action→completed 跳变触发）。"""
        prefix = self._auto_uid(TODO_KIND_REPLACE)
        if uid != prefix and not uid.startswith(prefix + "_"):
            return
        if new_status != TODO_STATUS_COMPLETED:
            return
        if old_status == TODO_STATUS_COMPLETED:
            return
        self.async_mark_replaced(uid)

    # ---- 待办同步 ----
    @callback
    def _sync_todos(self,
        newly_triggered: tuple[str, ...],
        newly_resolved: tuple[str, ...],
    ) -> None:
        """每个触发实体各生成一条「更换」待办（差集驱动 + 内存恢复兜底）。"""
        # 解绑清理用「展开后的实际实体集合」：绑定群组（group.xxx → 成员）时，
        # 触发成员待办不应被误判为「已解绑」而删除。
        bound = set(self.resolved_source_entities())
        replace_prefix = self._auto_uid(TODO_KIND_REPLACE) + "_"
        # 0. 内存恢复兜底：当前触发但缺 needs_action 待办 → 补建
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "replace"
        ):
            for entity_id in self._current_triggered.members:
                uid = self._auto_uid(TODO_KIND_REPLACE, entity_id)
                existing = self._todos.get(uid)
                if existing is None or existing["status"] != TODO_STATUS_NEEDS_ACTION:
                    self._upsert_auto_todo(
                        uid,
                        self._replace_summary(entity_id),
                        TODO_STATUS_NEEDS_ACTION,
                        self._replace_description(entity_id),
                    )
        # 1. 新增触发：创建或回弹为 needs_action
        for entity_id in newly_triggered:
            uid = self._auto_uid(TODO_KIND_REPLACE, entity_id)
            summary = self._replace_summary(entity_id)
            description = self._replace_description(entity_id)
            self._upsert_auto_todo(
                uid, summary, TODO_STATUS_NEEDS_ACTION, description
            )
        # 2. 本轮恢复正常：已存在对应待办 → completed（保留历史）
        for entity_id in newly_resolved:
            uid = self._auto_uid(TODO_KIND_REPLACE, entity_id)
            if uid in self._todos and (
                self._todos[uid]["status"] == TODO_STATUS_NEEDS_ACTION
            ):
                self._upsert_auto_todo(
                    uid,
                    self._todos[uid].get("summary"),
                    TODO_STATUS_COMPLETED,
                    self._todos[uid].get("description"),
                )
        # 3. 解绑清理：独立扫描 _todos 前缀，bound 外实体 → 删除（不是完成）
        for uid, todo in list(self._todos.items()):
            if not uid.startswith(replace_prefix):
                continue
            entity_id = uid[len(replace_prefix):]
            if entity_id not in bound:
                self._todos.pop(uid, None)

    # ---- 通知（基于触发集合差集，不再读 alert_status 单 bit）----
    @callback
    def async_subscribe(self) -> Callable[[], None] | None:
        """订阅绑定实体状态变化，即时刷新（手动改值即时检测跳变通知）。"""
        entities = list(
            set(self.source_entities)
            | set(self.resolved_source_entities())
        )
        if not entities:
            return None
        return async_track_state_change_event(
            self.hass, entities, self._on_state_change
        )

    @callback
    def _on_state_change(self, event: Any) -> None:
        """绑定实体状态变化 → 请求刷新（触发差集管线：评估 → diff → 待办/通知）。"""
        self.hass.async_create_task(self.async_request_refresh())

    def triggered_pairs(self) -> list[tuple[str, str, str]]:
        """越过阈值实体的 (显示名, 状态值, 单位)；显示名取快照或实体名。"""
        snapshots = {
            snap["entity_id"]: snap
            for snap in self.source_snapshots
            if snap.get("entity_id")
        }
        pairs: list[tuple[str, str, str]] = []
        for entity_id, value in zip(
            self.triggered_entities(), self.triggered_values()
        ):
            snapshot = snapshots.get(entity_id, {})
            state = self.hass.states.get(entity_id)
            state_name = (
                getattr(state, "name", None)
                or (state.attributes.get("friendly_name") if state else None)
            )
            # friendly_name 优先（用户自定义实体名），其次设备名/型号，最后实体 id
            display = (
                state_name
                or snapshot.get("device_name")
                or snapshot.get("device_model")
                or entity_id
            )
            value_text = _to_float(value) if value is not None else None
            value_str = f"{value_text:g}" if value_text is not None else "-"
            pairs.append((display, value_str, self.threshold_unit or ""))
        return pairs

    def alert_text(self, style: str) -> str:
        """按样式生成消息文案（多设备逐行）。
        human：「{设备名} 请更换耗材。」（话术走翻译键，通用文案）；
        value：「{设备名} {当前值}{单位}」。
        triggered_pairs() 已收敛到单一事实源。
        """
        lines: list[str] = []
        for display, value, unit in self.triggered_pairs():
            if style == NOTIFY_STYLE_VALUE:
                lines.append(f"{display} {value}{unit}")
            else:
                lines.append(
                    f"{display} {self._notify_text(NOTIFY_TEXT_REPLACE_NEEDED)}"
                )
        return "\n".join(lines) or self.title