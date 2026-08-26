"""耗材管理器 协调器平台。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from typing import Any, Callable, Literal
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN, LOGGER, CONF_ENTRY_TYPE, CONF_ITEM_ID,
    CONF_ITEM_NAME, CONF_ITEM_TYPE, CONF_LAST_TRIGGERED_SIG, CONF_MODEL,
    CONF_NOTIFY_MODE, CONF_NOTIFY_STYLE, CONF_QUANTITY, CONF_STOCK_ITEMS,
    CONF_STOCK_THRESHOLD, CONF_UNIT, ENTRY_SORT_PREFIXES, ENTRY_TYPE_NOTIFICATION,
    ENTRY_TYPE_STOCK, NOTIFY_MODE_REALTIME, NOTIFY_MODE_SCHEDULED, NOTIFY_STYLE_HUMAN,
    NOTIFY_STYLE_VALUE, NOTIFY_TEXT_DESC_THRESHOLD, NOTIFY_TEXT_LOW_STOCK, OPERATOR_EQUAL,
    OPERATOR_GREATER_THAN, OPERATOR_LESS_THAN, THRESHOLD_TYPE_LIFETIME_PERCENT, THRESHOLD_TYPE_NUMERIC,
    TIME_UNIT_TO_HOURS, TODO_KIND_PURCHASE, STATE_OK, STATE_LOW_STOCK,
    STATE_REPLACE_NEEDED, TODO_STATUS_NEEDS_ACTION, TODO_STATUS_COMPLETED, UPDATE_INTERVAL_SECONDS,
)

from .coordinator_type import ConsumableTypeCoordinator
from .library import Library, TypeMeta
from .user_library import async_load_library
from .notifications import (
    async_send_notification,
    find_notification_config,
    sanitize_notification_id,
)

# 复合状态集（由 const 基础状态派生；供 sensor.py 从 .coordinator 导入）
STOCK_STATES: tuple[str, ...] = (STATE_OK, STATE_LOW_STOCK)
REPLACE_STATES: tuple[str, ...] = (STATE_OK, STATE_REPLACE_NEEDED)

@dataclass(frozen=True)
class TriggeredSet:
    """单次阈值评估产生的触发集合快照（单一事实源）。"""
    kind: Literal["replace", "low_stock"]
    members: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # 排序列表后重新冻结赋值（不可变对象用 object.__setattr__ 绕开冻结）
        object.__setattr__(self, "members", tuple(sorted(set(self.members))))

    def __bool__(self) -> bool:
        return bool(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def __contains__(self, item: str) -> bool:
        return item in self.members

    def __sub__(self, other: "TriggeredSet") -> tuple[str, ...]:
        """差集 self - other：本次新增触发的成员（通知补送候选）。"""
        return tuple(sorted(set(self.members) - set(other.members)))

    def __rsub__(self, other: "TriggeredSet") -> tuple[str, ...]:
        """反差集 other - self：本次恢复正常的成员（自动完成待办）。"""
        return tuple(sorted(set(other.members) - set(self.members)))

    def signature(self) -> str:
        """持久化基线：len + 排序拼接，稳定且可读；空集返回 "0:"。"""
        return f"{len(self.members)}:{"|".join(self.members)}"

    @classmethod
    def from_signature(
        cls,
        kind: Literal["replace", "low_stock"],
        sig: str | None,
    ) -> "TriggeredSet":
        """从持久化签名反解（kind 由协调器子类提供）。 """
        if not sig:
            return cls(kind=kind)
        try:
            prefix, _, rest = sig.partition(":")
            n = int(prefix)
            members = tuple(x for x in rest.split("|") if x) if rest else ()
            if len(members) != n:
                return cls(kind=kind)
            return cls(kind=kind, members=members)
        except (ValueError, TypeError):
            return cls(kind=kind)

    def overall_state(self) -> str:
        """兼容旧传感器状态枚举：ok 或对应异常态。"""
        if not self:
            return STATE_OK
        return (
            STATE_REPLACE_NEEDED
            if self.kind == "replace"
            else STATE_LOW_STOCK
        )

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
    # 单位换算：时间类换算到小时（内部标准单位），剩余寿命% / 数值类不换算
    if unit is not None and threshold_type not in (
        THRESHOLD_TYPE_LIFETIME_PERCENT, THRESHOLD_TYPE_NUMERIC
    ):
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
        # 触发集合基线：None 表示尚未从 options 反解（首次/reload 时用）
        self._prev_triggered: TriggeredSet | None = None
        # 单一事实源：当前刷新计算出的触发集合快照（所有下游只读它）
        # None = 尚未跑过任何刷新（占位符），属性读取需 fallback 到实时评估。
        # 刷新第一时间被 _compute_triggered() 重写为真实 TriggeredSet。
        self._current_triggered: TriggeredSet | None = None
        # 首次基线标记：setup 期刷新后翻转 True，运行期才持久化（避免 setup 写 options 死循环）
        self._baseline_established: bool = False
        self.alert_pending: bool = False
        # 群组展开后的叶子实体缓存：评估阶段一次性重算，下游统一读取
        # （None = 尚未解析，首次访问时即时展开兜底）
        self._resolved_entities: list[str] | None = None

    @property
    def _trigger_kind(self) -> Literal["replace", "low_stock"]:
        """该协调器的触发类型（由子类 override，BaseCoordinator 默认 replace 不触发具体逻辑）。"""
        return "replace"

    # ---- 子类实现：唯一一次评估入口（每次刷新只调用一次）----
    def _compute_triggered(self) -> TriggeredSet:
        """计算当前轮的触发集合快照。"""
        return TriggeredSet(kind=self._trigger_kind)

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

    @callback
    def async_subscribe(self) -> Callable[[], None] | None:
        """建立运行时订阅（子类按需重写；库存/通知条目不订阅）。

        返回取消订阅回调；由 async_setup_entry 通过 entry.async_on_unload
        统一注册清理，避免分散的取消逻辑与潜在内存泄漏。
        """
        return None

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

    def _md_kv(self, key: str, value: str) -> str:
        """Markdown 键值行：**标签**分隔符值（待办描述美化，待办卡片支持 Markdown）。"""
        return f"**{self._notify_text(key)}**{self._label_sep()}{value}"

    def _auto_uid(self, kind: str, suffix: str = "") -> str:
        """自动待办 uid（稳定，用于去重与状态恢复）。"""
        base = f"{self._entry.entry_id}_{kind}"
        return f"{base}_{suffix}" if suffix else base

    def _completed_time(self, status: str) -> datetime | None:
        """按状态返回完成时间：completed 时为当前 UTC 时间，否则为 None。"""
        return datetime.now(timezone.utc) if status == TODO_STATUS_COMPLETED else None

    def _upsert_auto_todo(self,
        uid: str,
        summary: str,
        status: str,
        description: str | None = None,
    ) -> None:
        completed = self._completed_time(status)
        if uid in self._todos:
            self._todos[uid]["status"] = status
            self._todos[uid]["summary"] = summary
            self._todos[uid]["description"] = description
            self._todos[uid]["completed"] = completed
        else:
            self._todos[uid] = {
                "uid": uid,
                "summary": summary,
                "status": status,
                "due": None,
                "description": description,
                "completed": completed,
            }

    @callback
    def _complete_todo(self, uid: str) -> None:
        if uid in self._todos:
            self._todos[uid]["status"] = TODO_STATUS_COMPLETED
            self._todos[uid]["completed"] = datetime.now(timezone.utc)

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
            "completed": self._completed_time(status),
        }

    @callback
    def async_delete_todos(self, uids: set[str]) -> None:
        for uid in uids:
            self._todos.pop(uid, None)

    def get_todo_status(self, uid: str) -> str | None:
        """读取待办状态（公开接口，供 todo 平台查询原状态避免破坏封装）。"""
        todo = self._todos.get(uid)
        return todo["status"] if todo else None

    @callback
    def async_update_todo_fields(self,
        uid: str,
        **fields: Any,
    ) -> None:
        """按字段更新待办（仅覆盖传入的非 None 字段，未传字段保留原值）。"""
        if uid not in self._todos:
            return
        for key, value in fields.items():
            if value is not None:
                self._todos[uid][key] = value

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
    def _sync_todos(self,
        newly_triggered: tuple[str, ...],
        newly_resolved: tuple[str, ...],
    ) -> None:
        """按差集同步自动待办（子类按需重写，默认无自动待办）。"""
        _ = newly_triggered, newly_resolved
        return

    # ---- 通知（基于触发集合差集）----
    def alert_text(self, style: str) -> str:
        """按样式生成告警消息文案（公开接口，供通知平台与定时合并推送调用）。"""
        return self.title

    async def _async_send_alert(self,
        newly_triggered: tuple[str, ...] | None = None,
    ) -> None:
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
            self.alert_text(
                config.get(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN)
            ),
            notification_id=f"{DOMAIN}_{sanitize_notification_id(self.entry_id)}",
        )

    async def _async_notify_on_trigger_diff(self,
        newly_triggered: tuple[str, ...],
        is_first_baseline: bool,
    ) -> None:
        """基于触发集合差集的通知判定（阈值评估结果本身作为触发器）。"""
        new = self._current_triggered
        if is_first_baseline:
            if not new:
                self.alert_pending = False
            return
        if newly_triggered:
            await self._async_send_alert(newly_triggered=newly_triggered)
        if not new:
            self.alert_pending = False

    def _persist_alert_baseline(self, new_triggered: TriggeredSet) -> None:
        """持久化触发集合签名（仅运行期且变化时写 options，供 reload 后恢复基线）。"""
        if not self._baseline_established:
            return
        sig = new_triggered.signature()
        if self._entry.options.get(CONF_LAST_TRIGGERED_SIG) == sig:
            return
        options = dict(self._entry.options)
        options[CONF_LAST_TRIGGERED_SIG] = sig
        self._write_options(options)

    async def _async_update_data(self) -> None:
        """单次刷新唯一执行路径。"""
        # ---同步最新合并库（内置+用户；按文件改动缓存，未变动不重解析）---
        if getattr(self, "_auto_reload_library", False):
            self.update_library(await async_load_library(self.hass))
        # --- 唯一一次评估（所有下游只读这份快照）---
        new_triggered = self._compute_triggered()
        self._current_triggered = new_triggered
        # --- 恢复旧基线（内存 None = 首次或 reload）---
        if self._prev_triggered is None:
            self._prev_triggered = TriggeredSet.from_signature(
                kind=self._trigger_kind,
                sig=self._entry.options.get(CONF_LAST_TRIGGERED_SIG),
            )
        old_triggered = self._prev_triggered
        is_first_baseline = (
            not self._baseline_established
            and self._entry.options.get(CONF_LAST_TRIGGERED_SIG) is None
        )
        # --- 纯集合 diff（零副作用）---
        newly_triggered = new_triggered - old_triggered
        newly_resolved = old_triggered - new_triggered
        # --- 同步待办（独立 try，异常不吞通知）---
        try:
            self._sync_todos(newly_triggered, newly_resolved)
            LOGGER.debug(
                "[%s] _sync_todos done: %d todo(s) in dict, "
                "newly_triggered=%d, newly_resolved=%d, uids=%s",
                self.title, len(self._todos),
                len(newly_triggered), len(newly_resolved),
                sorted(self._todos.keys()),
            )
        except Exception:  # noqa: BLE001 - 日志兜底，不让异常串到刷新管线
            LOGGER.exception("[%s] _sync_todos failed", self.title)
        # --- 通知判定（独立 try，异常不影响待办）---
        try:
            await self._async_notify_on_trigger_diff(
                newly_triggered, is_first_baseline
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("[%s] notify step failed", self.title)
        # ---推进基线（内存 + 持久化）---
        self._prev_triggered = new_triggered
        self._persist_alert_baseline(new_triggered)
        # setup 期翻转：后续刷新才持久化
        self._baseline_established = True
        return None

class StockCoordinator(BaseCoordinator):
    """库存条目协调器：管理全部自定义库存项。"""

    @property
    def _trigger_kind(self) -> Literal["replace", "low_stock"]:
        return "low_stock"

    def _compute_triggered(self) -> TriggeredSet:
        """库存触发集合评估：沿用现有 low_items() 逻辑（显式操作驱动，天然稳定）。"""
        return TriggeredSet(
            kind="low_stock",
            members=tuple(self._eval_low_items()),
        )

    def _eval_low_items(self) -> list[str]:
        """低库存项评估（评估阶段唯一调用；下游只读 _current_triggered）。"""
        return [item_id for item_id in self.item_ids if self.is_low(item_id)]

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
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "low_stock"
        ):
            return list(self._current_triggered.members)
        return self._eval_low_items()

    def items_for_type(self, cons_type: str) -> list[str]:
        """按关联耗材类型查库存项（供标记更换时自动扣减）。"""
        return [
            item[CONF_ITEM_ID]
            for item in self.items
            if item.get(CONF_ITEM_TYPE) == cons_type
        ]

    # ---- 实体属性（全部收敛到单一事实源）----
    @property
    def stock_status(self) -> str:
        """库存汇总状态：直接从 TriggeredSet 取整体状态。"""
        if self._current_triggered is None:
            return STATE_LOW_STOCK if self._eval_low_items() else STATE_OK
        if self._current_triggered.kind != "low_stock":
            return STATE_LOW_STOCK if self._eval_low_items() else STATE_OK
        return self._current_triggered.overall_state()

    def item_attributes(self, item_id: str) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "consumable_type": (self.item(item_id) or {}).get(CONF_ITEM_TYPE),
            "unit": self.unit(item_id),
            "stock_threshold": self.stock_threshold(item_id),
            "low_stock": self.is_low(item_id),
        }

    def status_attributes(self) -> dict[str, Any]:
        low_ids: list[str]
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "low_stock"
        ):
            low_ids = list(self._current_triggered.members)
        else:
            low_ids = self._eval_low_items()
        return {
            "item_count": len(self.item_ids),
            "low_items": [self.item_name(i) for i in low_ids],
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
                f"**{label}**{self._label_sep()}{qty} {unit} / "
                f"{threshold_label} {threshold} {unit}"
            )
        return "\n".join(lines) if lines else None

    @callback
    def _sync_todos(self,
        newly_triggered: tuple[str, ...],
        newly_resolved: tuple[str, ...],
    ) -> None:
        """每个低库存项独立一条购买待办；补齐后该条自动完成。"""
        # 内存恢复兜底：当前触发但缺 needs_action 待办 → 补建
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "low_stock"
        ):
            for item_id in self._current_triggered.members:
                uid = self._auto_uid(TODO_KIND_PURCHASE, item_id)
                existing = self._todos.get(uid)
                if existing is None or existing["status"] != TODO_STATUS_NEEDS_ACTION:
                    self._upsert_auto_todo(
                        uid,
                        f"{self._label(TODO_KIND_PURCHASE)} {self.item_name(item_id)}",
                        TODO_STATUS_NEEDS_ACTION,
                        self._purchase_description(item_id),
                    )
        # 新增触发：创建或回弹为 needs_action
        for item_id in newly_triggered:
            uid = self._auto_uid(TODO_KIND_PURCHASE, item_id)
            self._upsert_auto_todo(
                uid,
                f"{self._label(TODO_KIND_PURCHASE)} {self.item_name(item_id)}",
                TODO_STATUS_NEEDS_ACTION,
                self._purchase_description(item_id),
            )
        # 本轮恢复正常：已存在的对应待办 → completed（不删，保留历史）
        for item_id in newly_resolved:
            uid = self._auto_uid(TODO_KIND_PURCHASE, item_id)
            if uid in self._todos and (
                self._todos[uid]["status"] == TODO_STATUS_NEEDS_ACTION
            ):
                self._upsert_auto_todo(
                    uid,
                    self._todos[uid]["summary"],
                    TODO_STATUS_COMPLETED,
                    self._todos[uid].get("description"),
                )
        # 清理升级遗留的合并版待办（无后缀的旧格式）
        self._todos.pop(self._auto_uid(TODO_KIND_PURCHASE), None)

    def alert_text(self, style: str) -> str:
        """按样式生成消息文案（多低库存项逐行）。"""
        lines: list[str] = []
        low_ids: list[str]
        if (
            self._current_triggered is not None
            and self._current_triggered.kind == "low_stock"
        ):
            low_ids = list(self._current_triggered.members)
        else:
            low_ids = self._eval_low_items()
        for item_id in low_ids:
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
        coordinator: BaseCoordinator = StockCoordinator(hass, entry, labels)
    elif entry_type == ENTRY_TYPE_NOTIFICATION:
        coordinator = BaseCoordinator(hass, entry, labels)
    else:
        coordinator = ConsumableTypeCoordinator(
            hass, entry, labels, type_meta, library
        )
        # 开启刷新期自动重载合并库：手改用户库/内置库后无需重载条目即生效
        coordinator._auto_reload_library = True
    # 定时轮询兜底：实体值经事件订阅即时刷新；此处保证即使漏订阅也能周期
    # 性检测跳变。通知条目（BaseCoordinator 直接充当）无业务状态，无需轮询。
    if entry_type != ENTRY_TYPE_NOTIFICATION:
        coordinator.update_interval = timedelta(seconds=UPDATE_INTERVAL_SECONDS)
    return coordinator

@dataclass
class ConsumableManagerData:
    """每个配置条目的运行时数据（存于 entry.runtime_data）。"""
    coordinator: BaseCoordinator
    # 条目建立时的实体集合签名，用于判断配置变更是否需要重载
    entity_signature: tuple[str, ...] = ()