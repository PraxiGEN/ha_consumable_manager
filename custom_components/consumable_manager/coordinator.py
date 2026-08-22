"""耗材管理器 协调器平台。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import as_local

from .const import (
    DOMAIN, LOGGER, CONF_ENTRY_TYPE, CONF_ITEM_ID, CONF_ITEM_NAME,
    CONF_ITEM_TYPE, CONF_LAST_REPLACED, CONF_MODEL, CONF_NOTIFY_MODE,
    CONF_NOTIFY_STYLE, CONF_QUANTITY, CONF_SOURCE_ENTITIES, CONF_STOCK_ITEMS,
    CONF_STOCK_THRESHOLD, CONF_THRESHOLD, CONF_THRESHOLD_OPERATOR, CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT, CONF_UNIT, DEFAULT_THRESHOLD, DEFAULT_THRESHOLD_TYPE,
    DEFAULT_THRESHOLD_UNIT, ENTRY_SORT_PREFIXES, ENTRY_TYPE_NOTIFICATION, ENTRY_TYPE_STOCK,
    NOTIFY_MODE_REALTIME, NOTIFY_MODE_SCHEDULED, NOTIFY_STYLE_HUMAN, NOTIFY_STYLE_VALUE,
    NOTIFY_TEXT_CONSUMABLES, NOTIFY_TEXT_DESC_AREA, NOTIFY_TEXT_DESC_DEVICE,
    NOTIFY_TEXT_DESC_ENTITY, NOTIFY_TEXT_DESC_SPECS, NOTIFY_TEXT_DESC_THRESHOLD,
    NOTIFY_TEXT_LAST_REPLACED,
    NOTIFY_TEXT_LOW_STOCK, NOTIFY_TEXT_REPLACE_NEEDED, NOTIFY_TEXT_UNKNOWN,
    OPERATOR_EQUAL, OPERATOR_GREATER_THAN, OPERATOR_LESS_THAN, THRESHOLD_DEFAULT_OPERATOR,
    THRESHOLD_TYPE_LIFETIME_PERCENT, TIME_UNIT_TO_HOURS, TODO_KIND_PURCHASE, TODO_KIND_REPLACE,
)
from .library import Library, TypeMeta
from .notifications import (
    async_send_notification,
    find_notification_config,
    sanitize_notification_id,
)

# ---- 通知状态（枚举传感器，状态值由 translations 翻译）----
STATE_OK = "ok"
STATE_LOW_STOCK = "low_stock"  # 库存条目：有库存项低于阈值
STATE_REPLACE_NEEDED = "replace_needed"  # 耗材类型条目：有绑定实体越过阈值
STOCK_STATES: tuple[str, ...] = (STATE_OK, STATE_LOW_STOCK)
REPLACE_STATES: tuple[str, ...] = (STATE_OK, STATE_REPLACE_NEEDED)
# ---- 待办状态 ----
TODO_STATUS_NEEDS_ACTION = "needs_action"
TODO_STATUS_COMPLETED = "completed"

def _to_float(value: str | float | int | None) -> float | None:
    """把实体状态安全转 float，失败返回 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def evaluate_threshold(threshold_type: str,
    threshold: float | None,
    operator: str,
    values: list[float | None],
    unit: str | None = None,
) -> bool:
    """判断是否越过阈值（纯函数，便于单元测试）。"""
    if threshold is None:
        return False
    # 单位换算：时间类换算到小时（内部标准单位），剩余寿命% 不换算
    if unit is not None and threshold_type != THRESHOLD_TYPE_LIFETIME_PERCENT:
        threshold = threshold * TIME_UNIT_TO_HOURS.get(unit, 1.0)
    for value in values:
        if value is None:
            continue
        if operator == OPERATOR_LESS_THAN and value < threshold:
            return True
        if operator == OPERATOR_GREATER_THAN and value > threshold:
            return True
        if operator == OPERATOR_EQUAL and value == threshold:
            return True
    return False

class BaseCoordinator(DataUpdateCoordinator[None]):
    """协调器基类：条目访问、设备信息、待办读写。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        labels: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            hass, LOGGER, name=f"{DOMAIN}_{entry.entry_id}", config_entry=entry
        )
        self._entry = entry
        self._labels = labels or {}
        self._todos: dict[str, dict[str, Any]] = {}
        self._prev_alert_status: str | None = None
        self.alert_pending: bool = False

    @property
    def entry(self) -> ConfigEntry:
        return self._entry

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id

    @property
    def entry_type(self) -> str:
        """条目类型：stock（库存条目）或某个耗材类型。"""
        return self._entry.data.get(CONF_ENTRY_TYPE, "")

    @property
    def title(self) -> str:
        """展示用标题：剥离排序前缀（前缀只用于 HA 条目列表置顶）。"""
        for prefix in ENTRY_SORT_PREFIXES.values():
            if self._entry.title.startswith(prefix):
                return self._entry.title[len(prefix) :]
        return self._entry.title

    @property
    def options(self) -> dict[str, Any]:
        """条目配置（全部由配置界面写入 entry.options）。"""
        return dict(self._entry.options)

    @property
    def device_info(self) -> DeviceInfo:
        """条目设备信息（实体引用后自动注册设备）。"""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self.title,
            manufacturer="Consumable Manager",
            model=self.entry_type,
        )

    @property
    def entity_signature(self) -> tuple[str, ...]:
        """本条目当前应生成的实体键集合。

        默认无实体（通知条目直接用 BaseCoordinator）。
        配置变更后由 update_listener 比对：集合变化则重载条目
        （增删实体），仅数值变化则刷新（不重建实体）。
        """
        return ()

    @callback
    def _write_options(self, options: dict[str, Any]) -> None:
        """写回条目配置（持久化，重启不丢）。"""
        self.hass.config_entries.async_update_entry(
            self._entry, options=options
        )

    # ---- 待办事项（存 dict，todo.py 负责转 TodoItem）----
    def todo_dicts(self) -> list[dict[str, Any]]:
        return list(self._todos.values())

    def _label(self, kind: str) -> str:
        """待办动词（本地化，取不到时回退 kind 键，不硬编码）。"""
        return self._labels.get(kind, kind)

    def _notify_text(self, key: str) -> str:
        """通知 / 待办固定话术（本地化，取不到时回退键，不硬编码）。"""
        return self._labels.get(key, key)

    def _label_sep(self) -> str:
        """标签与内容分隔符：中文全角冒号，其他语言半角冒号加空格。"""
        language = str(self.hass.config.language)
        return "：" if language.lower().startswith("zh") else ": "

    def _auto_uid(self, kind: str, suffix: str = "") -> str:
        """自动待办 uid（稳定，用于去重与状态恢复）。"""
        base = f"{self._entry.entry_id}_{kind}"
        return f"{base}_{suffix}" if suffix else base

    def _upsert_auto_todo(self,
        uid: str,
        summary: str,
        status: str,
        description: str | None = None,
    ) -> None:
        if uid in self._todos:
            self._todos[uid]["status"] = status
            self._todos[uid]["summary"] = summary
            self._todos[uid]["description"] = description
        else:
            self._todos[uid] = {
                "uid": uid,
                "summary": summary,
                "status": status,
                "due": None,
                "description": description,
            }

    @callback
    def _complete_todo(self, uid: str) -> None:
        if uid in self._todos:
            self._todos[uid]["status"] = TODO_STATUS_COMPLETED

    @callback
    def async_upsert_todo(
        self,
        uid: str | None,
        summary: str,
        status: str,
        due: date | datetime | None = None,
        description: str | None = None,
    ) -> None:
        """创建或更新待办；uid 为空时分配新 uid。"""
        if not uid:
            uid = f"{self._entry.entry_id}_custom_{uuid4().hex[:8]}"
        self._todos[uid] = {
            "uid": uid,
            "summary": summary,
            "status": status,
            "due": due,
            "description": description,
        }

    @callback
    def async_delete_todos(self, uids: set[str]) -> None:
        for uid in uids:
            self._todos.pop(uid, None)

    @callback
    def async_set_todo_due(self, uid: str, due: date | datetime | None) -> None:
        if uid in self._todos:
            self._todos[uid]["due"] = due

    @callback
    def async_on_todo_completed(self,
        uid: str,
        old_status: str | None,
        new_status: str,
    ) -> None:
        """待办被勾选完成时回调（子类可重写；基类默认无操作）。
        用于「勾选待办 = 已更换」联动：库存条目不重写，耗材类型条目重写。
        """
        return

    @callback
    def _sync_todos(self) -> None:
        """按当前状态同步自动待办（子类按需重写，默认无自动待办）。"""
        return

    # ---- 通知（状态跳变「正常 → 异常」时发一次）----
    @property
    def alert_status(self) -> str:
        """当前告警状态（STATE_OK 或对应异常态；子类按需重写）。"""
        return STATE_OK

    def _alert_text(self, style: str) -> str:
        """按样式生成告警消息文案（子类按需重写）。"""
        return self.title

    async def _async_send_alert(self) -> None:
        """按生效配置发送；实时立即单发，定时只置待推送标记。"""
        config = find_notification_config(self.hass, self.options)
        if config is None:
            return
        if config.get(CONF_NOTIFY_MODE, NOTIFY_MODE_REALTIME) == (
            NOTIFY_MODE_SCHEDULED
        ):
            self.alert_pending = True
            return
        await async_send_notification(
            self.hass,
            config,
            self.title,
            self._alert_text(
                config.get(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN)
            ),
            notification_id=f"{DOMAIN}_{sanitize_notification_id(self.entry_id)}",
        )

    async def _async_notify_on_transition(self) -> None:
        """状态跳变检测：仅「正常 → 异常」瞬间发一次（首次不建基线、持续不重复）。"""
        status = self.alert_status
        prev = self._prev_alert_status
        self._prev_alert_status = status
        if prev is None or prev != STATE_OK or status == STATE_OK:
            if status == STATE_OK:
                self.alert_pending = False
            return
        await self._async_send_alert()

    async def _async_update_data(self) -> None:
        """每次刷新：同步自动待办 + 检查告警跳变通知。"""
        self._sync_todos()
        await self._async_notify_on_transition()
        return None

class StockCoordinator(BaseCoordinator):
    """库存条目协调器：管理全部自定义库存项。"""

    # ---- 库存项读取 ----
    @property
    def items(self) -> list[dict[str, Any]]:
        """全部库存项（顺序即添加顺序）。"""
        return list(self._entry.options.get(CONF_STOCK_ITEMS, []))

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(item[CONF_ITEM_ID] for item in self.items)

    @property
    def entity_signature(self) -> tuple[str, ...]:
        """每个库存项一个实体；有库存项时另加汇总实体。"""
        ids = self.item_ids
        return (*ids, "stock_status") if ids else ()

    def item(self, item_id: str) -> dict[str, Any] | None:
        for item in self.items:
            if item[CONF_ITEM_ID] == item_id:
                return item
        return None

    def item_name(self, item_id: str) -> str:
        item = self.item(item_id)
        return item.get(CONF_ITEM_NAME, item_id) if item else item_id

    def quantity(self, item_id: str) -> int:
        """库存数量（可为负，负数表示欠货）。"""
        item = self.item(item_id)
        if item is None:
            return 0
        try:
            return int(item.get(CONF_QUANTITY, 0))
        except (TypeError, ValueError):
            return 0

    def stock_threshold(self, item_id: str) -> int | None:
        item = self.item(item_id)
        if item is None:
            return None
        try:
            return int(item[CONF_STOCK_THRESHOLD])
        except (KeyError, TypeError, ValueError):
            return None

    def unit(self, item_id: str) -> str | None:
        item = self.item(item_id)
        return item.get(CONF_UNIT) or None if item else None

    def is_low(self, item_id: str) -> bool:
        """是否低于库存阈值。"""
        threshold = self.stock_threshold(item_id)
        if threshold is None:
            return False
        return self.quantity(item_id) < threshold

    def low_items(self) -> list[str]:
        """低于阈值的库存项 id 列表。"""
        return [item_id for item_id in self.item_ids if self.is_low(item_id)]

    def items_for_type(self, cons_type: str) -> list[str]:
        """按关联耗材类型查库存项（供标记更换时自动扣减）。"""
        return [
            item[CONF_ITEM_ID]
            for item in self.items
            if item.get(CONF_ITEM_TYPE) == cons_type
        ]

    # ---- 实体属性 ----
    @property
    def stock_status(self) -> str:
        """库存汇总状态：有任一项低于阈值即为库存不足。"""
        return STATE_LOW_STOCK if self.low_items() else STATE_OK

    def item_attributes(self, item_id: str) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "consumable_type": (self.item(item_id) or {}).get(CONF_ITEM_TYPE),
            "unit": self.unit(item_id),
            "stock_threshold": self.stock_threshold(item_id),
            "low_stock": self.is_low(item_id),
        }

    def status_attributes(self) -> dict[str, Any]:
        return {
            "item_count": len(self.item_ids),
            "low_items": [self.item_name(i) for i in self.low_items()],
        }

    # ---- 写入（持久化到 entry.options）----
    @callback
    def async_set_quantity(self, item_id: str, value: int) -> None:
        """设置某库存项数量。"""
        items = self.items
        for item in items:
            if item[CONF_ITEM_ID] == item_id:
                item[CONF_QUANTITY] = value
                break
        else:
            return
        options = self.options
        options[CONF_STOCK_ITEMS] = items
        self._write_options(options)

    @callback
    def async_add_quantity(self, item_id: str, delta: int) -> None:
        """增减某库存项数量（delta 可为负，结果可为负表示欠货）。"""
        self.async_set_quantity(item_id, self.quantity(item_id) + delta)

    # ---- 待办同步 ----
    def _purchase_description(self, item_id: str | None = None) -> str | None:
        """购买待办描述：低库存项明细（名称（型号）：数量 / 阈值），标签走翻译键。
        item_id 为空时遍历所有低库存项（向后兼容）；否则只描述单个库存项。
        """
        lines: list[str] = []
        threshold_label = self._notify_text(NOTIFY_TEXT_DESC_THRESHOLD)
        for iid in ([item_id] if item_id else self.low_items()):
            item = self.item(iid)
            if item is None:
                continue
            name = item.get(CONF_ITEM_NAME) or item.get(CONF_MODEL) or iid
            model = item.get(CONF_MODEL)
            unit = item.get(CONF_UNIT, "")
            qty = item.get(CONF_QUANTITY, 0)
            threshold = item.get(CONF_STOCK_THRESHOLD, 0)
            label = f"{name}（{model}）" if model else name
            lines.append(
                f"{label}：{qty} {unit} / {threshold_label} "
                f"{threshold} {unit}"
            )
        return "\n".join(lines) if lines else None

    @callback
    def _sync_todos(self) -> None:
        """每个低库存项独立一条购买待办；补齐后该条自动完成（仍低则回弹提醒）。"""
        low = set(self.low_items())
        purchase_prefix = f"{self._entry.entry_id}_{TODO_KIND_PURCHASE}_"
        # 1. 低库存项：确保为 needs_action（不存在则创建，已存在则同步回提醒态）
        for item_id in low:
            uid = self._auto_uid(TODO_KIND_PURCHASE, item_id)
            self._upsert_auto_todo(
                uid,
                f"{self._label(TODO_KIND_PURCHASE)} {self.item_name(item_id)}",
                TODO_STATUS_NEEDS_ACTION,
                self._purchase_description(item_id),
            )
        # 2. 曾低但已补齐的项：将其 needs_action 待办标记完成（恢复不删，保留历史）
        for uid, todo in list(self._todos.items()):
            if not uid.startswith(purchase_prefix):
                continue
            item_id = uid[len(purchase_prefix):]
            if todo["status"] == TODO_STATUS_NEEDS_ACTION and item_id not in low:
                self._upsert_auto_todo(
                    uid, todo["summary"], TODO_STATUS_COMPLETED,
                    todo.get("description"),
                )
        # 3. 清理升级遗留的合并版待办（无后缀的旧格式）
        self._todos.pop(self._auto_uid(TODO_KIND_PURCHASE), None)

    # ---- 通知（低库存跳变：消息按样式生成）----
    @property
    def alert_status(self) -> str:
        """库存告警状态：任一库存项低于阈值即库存不足。"""
        return self.stock_status

    def _alert_text(self, style: str) -> str:
        """按样式生成消息文案（多低库存项逐行）。
        human：「{名称} 库存告急，请购买。」（话术走翻译键，通用文案）；
        value：「{名称} {数量}{单位}」。
        """
        lines: list[str] = []
        for item_id in self.low_items():
            name = self.item_name(item_id)
            if style == NOTIFY_STYLE_VALUE:
                lines.append(
                    f"{name} {self.quantity(item_id)}{self.unit(item_id) or ''}"
                )
            else:
                lines.append(
                    f"{name} {self._notify_text(NOTIFY_TEXT_LOW_STOCK)}"
                )
        return "\n".join(lines) or self.title

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

    @property
    def cons_type(self) -> str:
        """耗材类型（电池 / 滤芯 / …）。"""
        return self.entry_type

    @property
    def entity_signature(self) -> tuple[str, ...]:
        """未绑定任何实体时不生成实体。"""
        return ("replace_status",) if self.source_entities else ()

    # ---- 绑定实体（支持群组：运行时展开成员，动态跟随）----
    @property
    def source_snapshots(self) -> list[dict[str, Any]]:
        return list(self._entry.options.get(CONF_SOURCE_ENTITIES, []))

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
        """递归展开群组成员（防循环去重保序；每次重解析 = 动态跟随）。

        群组判定：`group.` 域实体，或任意域但状态携带非空 `entity_id`
        成员列表属性（group 集成生成的 sensor/其他域组实体均满足）。
        """
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
        """绑定实体展开群组后的实际实体（每次调用重新解析，动态跟随）。"""
        return self._expand_groups(self.source_entities)

    def bound_values(self) -> list[float | None]:
        """读取实际实体的实时值（缺失 / 不可用返回 None）。"""
        values: list[float | None] = []
        for entity_id in self.resolved_source_entities():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable", None):
                values.append(None)
                continue
            values.append(_to_float(state.state))
        return values

    def triggered_entities(self) -> list[str]:
        """当前越过阈值的实际实体（用于告知具体是哪台设备）。"""
        triggered: list[str] = []
        for entity_id, value in zip(
            self.resolved_source_entities(), self.bound_values()
        ):
            if evaluate_threshold(
                self.threshold_type,
                self.threshold,
                self.threshold_operator,
                [value],
                self.threshold_unit,
            ):
                triggered.append(entity_id)
        return triggered

    def triggered_values(self) -> list[float | None]:
        """越过阈值的实体对应值（与 triggered_entities 同序）。"""
        values = self.bound_values()
        return [
            value
            for entity_id, value in zip(
                self.resolved_source_entities(), values
            )
            if evaluate_threshold(
                self.threshold_type,
                self.threshold,
                self.threshold_operator,
                [value],
                self.threshold_unit,
            )
        ]

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
    def replace_status(self) -> str:
        """更换状态：任一绑定实体越过阈值即需要更换。"""
        if evaluate_threshold(
            self.threshold_type,
            self.threshold,
            self.threshold_operator,
            self.bound_values(),
            self.threshold_unit,
        ):
            return STATE_REPLACE_NEEDED
        return STATE_OK

    @property
    def last_replaced(self) -> str | None:
        return self._entry.options.get(CONF_LAST_REPLACED)

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
        return (
            f"{self._notify_text(NOTIFY_TEXT_LAST_REPLACED)}"
            f"{self._label_sep()}"
            f"{local.strftime('%Y-%m-%d %H:%M')}"
        )

    def _consumables_label(self) -> str | None:
        """该类型耗材信息（来自库）：「耗材：名称（单位）、…」列表。"""
        library = self._library
        if library is None:
            return None
        items = library.by_type(self.cons_type)
        if not items:
            return None
        locale = self.hass.config.language
        names = "、".join(
            f"{c.display_name(locale)}（{c.unit}）" for c in items
        )
        return (
            f"{self._notify_text(NOTIFY_TEXT_CONSUMABLES)}"
            f"{self._label_sep()}{names}"
        )

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

    def _entity_consumables(self,
        snapshot: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """设备绑定的耗材（库内按 manufacturer+model 匹配）。

        返回 (耗材名称清单, 规格行)；未绑定或库未加载返回 (None, None)。
        """
        if self._library is None:
            return None, None
        items = self._library.find_compatible(
            snapshot.get("manufacturer"),
            snapshot.get("device_model"),
        )
        if not items:
            return None, None
        locale = self.hass.config.language
        names = "、".join(
            f"{c.display_name(locale)}（{c.unit}）" for c in items
        )
        specs = "；".join(
            f"{c.display_name(locale)}: "
            f"{json.dumps(c.meta, ensure_ascii=False)}"
            for c in items if c.meta
        )
        return names, specs or None

    def _replace_summary(self, entity_id: str) -> str:
        """更换待办标题：设备名称 + 「请更换耗材。」（通用话术，按语言）。

        约定：每个触发实体各生成一条待办，标题取其设备名，如
        「书房温湿度传感器 请更换耗材。」（多设备时不合并）。
        """
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
        display = (
            snapshot.get("device_name")
            or snapshot.get("device_model")
            or state_name
            or entity_id
        )
        return f"{display} {self._notify_text(NOTIFY_TEXT_REPLACE_NEEDED)}"

    def _replace_description(self, entity_id: str | None = None) -> str | None:
        """更换待办描述（单实体）：区域/设备/实体/耗材/规格；未绑定耗材显示
        「耗材：未知」。entity_id 为空时遍历所有触发实体；无触发实体则回退
        类型耗材信息，并附上次更换时间。
        """
        parts: list[str] = []
        snapshots = {
            snap["entity_id"]: snap
            for snap in self.source_snapshots
            if snap.get("entity_id")
        }
        for eid in ([entity_id] if entity_id else self.triggered_entities()):
            lines: list[str] = []
            if area := self._entity_area(eid):
                lines.append(
                    f"{self._notify_text(NOTIFY_TEXT_DESC_AREA)}"
                    f"{self._label_sep()}{area}"
                )
            state = self.hass.states.get(eid)
            state_name = (
                getattr(state, "name", None)
                or (state.attributes.get("friendly_name") if state else None)
            )
            snapshot = snapshots.get(eid, {})
            display = snapshot.get("device_name") or state_name
            if display:
                lines.append(
                    f"{self._notify_text(NOTIFY_TEXT_DESC_DEVICE)}"
                    f"{self._label_sep()}{display}"
                )
            lines.append(
                f"{self._notify_text(NOTIFY_TEXT_DESC_ENTITY)}"
                f"{self._label_sep()}{eid}"
            )
            cons_names, specs = self._entity_consumables(snapshot)
            if cons_names:
                lines.append(
                    f"{self._notify_text(NOTIFY_TEXT_CONSUMABLES)}"
                    f"{self._label_sep()}{cons_names}"
                )
            else:
                lines.append(
                    f"{self._notify_text(NOTIFY_TEXT_CONSUMABLES)}"
                    f"{self._label_sep()}"
                    f"{self._notify_text(NOTIFY_TEXT_UNKNOWN)}"
                )
            if specs:
                lines.append(
                    f"{self._notify_text(NOTIFY_TEXT_DESC_SPECS)}"
                    f"{self._label_sep()}{specs}"
                )
            parts.append("\n".join(lines))
        if not parts and (cons := self._consumables_label()):
            # 无触发实体（如刚恢复）：保留类型耗材信息
            parts.append(cons)
        if last := self._last_replaced_label():
            parts.append(last)
        return "\n\n".join(parts) if parts else None

    @callback
    def async_mark_replaced(self, uid: str | None = None) -> None:
        """标记已更换：记录时间、完成「更换」待办，并联动扣减关联类型的库存项。

        uid 为空时完成本条目全部自动更换待办（手动「标记全部已更换」）；
        传入具体 uid 时只完成对应实体的待办（勾选单条待办场景）。
        """
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
    def _sync_todos(self) -> None:
        """每个触发实体各生成一条「更换」待办（标题=设备名+请更换耗材）；

        实体越过阈值解除（恢复）后该条自动完成；实体解绑后清理遗留待办。
        """
        triggered = set(self.triggered_entities())
        bound = set(self.source_entities)
        prefix = self._auto_uid(TODO_KIND_REPLACE) + "_"
        # 为每个触发实体创建 / 刷新独立待办
        for entity_id in triggered:
            uid = self._auto_uid(TODO_KIND_REPLACE, entity_id)
            summary = self._replace_summary(entity_id)
            description = self._replace_description(entity_id)
            self._upsert_auto_todo(
                uid, summary, TODO_STATUS_NEEDS_ACTION, description
            )
        # 已存在的自动更换待办：解绑删除、恢复完成
        for uid, todo in list(self._todos.items()):
            if not uid.startswith(prefix):
                continue
            entity_id = uid[len(prefix):]
            if entity_id not in bound:
                self._todos.pop(uid, None)
                continue
            if entity_id not in triggered:
                self._upsert_auto_todo(
                    uid, todo.get("summary"), TODO_STATUS_COMPLETED,
                    todo.get("description"),
                )

    # ---- 通知（需更换跳变：消息按样式生成）----
    @property
    def alert_status(self) -> str:
        """更换告警状态：任一绑定实体越过阈值即需要更换。"""
        return self.replace_status

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
            display = (
                snapshot.get("device_name")
                or snapshot.get("device_model")
                or state_name
                or entity_id
            )
            value_text = _to_float(value) if value is not None else None
            value_str = f"{value_text:g}" if value_text is not None else "-"
            pairs.append((display, value_str, self.threshold_unit or ""))
        return pairs

    def _alert_text(self, style: str) -> str:
        """按样式生成消息文案（多设备逐行）。
        human：「{设备名} 请更换耗材。」（话术走翻译键，通用文案）；
        value：「{设备名} {当前值}{单位}」。
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

def _find_stock_coordinator(hass: HomeAssistant) -> StockCoordinator | None:
    """查找库存条目协调器（经 runtime_data 定位，供更换时扣减库存）。"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        # getattr 防御：加载过程中部分条目尚未写入 runtime_data
        data = getattr(entry, "runtime_data", None)
        if (
            isinstance(data, ConsumableManagerData)
            and entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_STOCK
        ):
            coordinator = data.coordinator
            if isinstance(coordinator, StockCoordinator):
                return coordinator
    return None

def build_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    labels: dict[str, str] | None = None,
    type_meta: TypeMeta | None = None,
    library: Library | None = None,
) -> BaseCoordinator:
    """按条目类型创建对应协调器（type_meta 为库类型元数据，自定义类型为 None）。"""
    entry_type = entry.data.get(CONF_ENTRY_TYPE)
    if entry_type == ENTRY_TYPE_STOCK:
        return StockCoordinator(hass, entry, labels)
    if entry_type == ENTRY_TYPE_NOTIFICATION:
        return BaseCoordinator(hass, entry, labels)
    return ConsumableTypeCoordinator(hass, entry, labels, type_meta, library)

@dataclass
class ConsumableManagerData:
    """每个配置条目的运行时数据（存于 entry.runtime_data）。"""
    coordinator: BaseCoordinator
    # 条目建立时的实体集合签名，用于判断配置变更是否需要重载
    entity_signature: tuple[str, ...] = ()
