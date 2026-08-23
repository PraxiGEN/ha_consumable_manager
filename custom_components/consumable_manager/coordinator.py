"""耗材管理器 协调器平台。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
import json
from typing import Any, Final, Literal
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import as_local

from .const import (
    DOMAIN, LOGGER, CONF_ENTRY_TYPE, CONF_ITEM_ID, CONF_ITEM_NAME,
    CONF_ITEM_TYPE, CONF_LAST_REPLACED, CONF_LAST_TRIGGERED_SIG, CONF_MODEL,
    CONF_NOTIFY_MODE, CONF_NOTIFY_STYLE, CONF_QUANTITY, CONF_SOURCE_ENTITIES,
    CONF_STOCK_ITEMS, CONF_STOCK_THRESHOLD, CONF_THRESHOLD, CONF_THRESHOLD_OPERATOR,
    CONF_THRESHOLD_TYPE, CONF_THRESHOLD_UNIT, CONF_UNIT, DEFAULT_THRESHOLD,
    DEFAULT_THRESHOLD_TYPE, DEFAULT_THRESHOLD_UNIT, ENTRY_SORT_PREFIXES,
    ENTRY_TYPE_NOTIFICATION, ENTRY_TYPE_STOCK, NOTIFY_MODE_REALTIME,
    NOTIFY_MODE_SCHEDULED, NOTIFY_STYLE_HUMAN, NOTIFY_STYLE_VALUE,
    NOTIFY_TEXT_CONSUMABLES, NOTIFY_TEXT_DESC_AREA, NOTIFY_TEXT_DESC_DEVICE,
    NOTIFY_TEXT_DESC_ENTITY, NOTIFY_TEXT_DESC_SPECS, NOTIFY_TEXT_DESC_THRESHOLD,
    NOTIFY_TEXT_LAST_REPLACED,
    NOTIFY_TEXT_LOW_STOCK, NOTIFY_TEXT_REPLACE_NEEDED, NOTIFY_TEXT_UNKNOWN,
    OPERATOR_EQUAL, OPERATOR_GREATER_THAN, OPERATOR_LESS_THAN, THRESHOLD_DEFAULT_OPERATOR,
    THRESHOLD_TYPE_LIFETIME_PERCENT, THRESHOLD_TYPE_NUMERIC, TIME_UNIT_TO_HOURS,
    TODO_KIND_PURCHASE, TODO_KIND_REPLACE,
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
# 协调器定时轮询兜底间隔（秒）：保证即使实体变化事件漏订阅也能周期性检测跳变
UPDATE_INTERVAL_SECONDS: Final[int] = 60
# ---- 待办状态 ----
TODO_STATUS_NEEDS_ACTION = "needs_action"
TODO_STATUS_COMPLETED = "completed"

@dataclass(frozen=True)
class TriggeredSet:
    """单次阈值评估产生的触发集合快照（单一事实源）。

    不可变；可直接 == 比较；集合运算纯函数，零副作用。
    members 永远排序后存储，因此不同来源的相同成员集合必然 ==。
    """

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
        """从持久化签名反解（kind 由协调器子类提供）。

        空/损坏/缺失 sig 返回空集合（等价于首次基线空集合），避免把
        「reload 前 ok」误判成「首次无基线」而补发通知。
        """
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
        self._unsub_state: Any | None = None

    @property
    def _trigger_kind(self) -> Literal["replace", "low_stock"]:
        """该协调器的触发类型（由子类 override，BaseCoordinator 默认 replace 不触发具体逻辑）。"""
        return "replace"

    # ---- 子类实现：唯一一次评估入口（每次刷新只调用一次）----
    def _compute_triggered(self) -> TriggeredSet:
        """计算当前轮的触发集合快照。

        BaseCoordinator 默认空集合（通知条目无业务状态）。
        StockCoordinator / ConsumableTypeCoordinator 各自重写为实际评估逻辑。
        """
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
    def async_subscribe(self) -> None:
        """建立运行时订阅（子类按需重写；库存/通知条目不订阅）。"""
        return

    @callback
    def async_unsubscribe(self) -> None:
        """取消实体状态订阅（同步；定时轮询由父类 async_shutdown 清理）。"""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

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
    def _sync_todos(self,
        newly_triggered: tuple[str, ...],
        newly_resolved: tuple[str, ...],
    ) -> None:
        """按差集同步自动待办（子类按需重写，默认无自动待办）。"""
        return

    # ---- 通知（基于触发集合差集）----
    def _alert_text(self, style: str) -> str:
        """按样式生成告警消息文案（子类按需重写）。"""
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
            self._alert_text(
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
        # --- 1. 唯一一次评估（所有下游只读这份快照）---
        new_triggered = self._compute_triggered()
        self._current_triggered = new_triggered
        # --- 2. 恢复旧基线（内存 None = 首次或 reload）---
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
        # --- 3. 纯集合 diff（零副作用）---
        newly_triggered = new_triggered - old_triggered
        newly_resolved = old_triggered - new_triggered
        # --- 4. 同步待办（独立 try，异常不吞通知）---
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
        # --- 5. 通知判定（独立 try，异常不影响待办）---
        try:
            await self._async_notify_on_trigger_diff(
                newly_triggered, is_first_baseline
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("[%s] notify step failed", self.title)
        # --- 6. 推进基线（内存 + 持久化）---
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
        """低于阈值的库存项 id 列表。

        对外统一读单一事实源 _current_triggered；尚未刷新时（setup 前 /
        测试构造后不 refresh 场景）退到实时评估保证兼容。
        """
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
        """库存汇总状态：直接从 TriggeredSet 取整体状态。

        第一轮刷新尚未完成（setup 期极短窗口 / 测试构造后不 refresh）时，
        兜底实时评估保证兼容。
        """
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
                f"{label}：{qty} {unit} / {threshold_label} "
                f"{threshold} {unit}"
            )
        return "\n".join(lines) if lines else None

    @callback
    def _sync_todos(self,
        newly_triggered: tuple[str, ...],
        newly_resolved: tuple[str, ...],
    ) -> None:
        """每个低库存项独立一条购买待办；补齐后该条自动完成。"""
        purchase_prefix = f"{self._entry.entry_id}_{TODO_KIND_PURCHASE}_"
        # 1. 新增触发：创建或回弹为 needs_action
        for item_id in newly_triggered:
            uid = self._auto_uid(TODO_KIND_PURCHASE, item_id)
            self._upsert_auto_todo(
                uid,
                f"{self._label(TODO_KIND_PURCHASE)} {self.item_name(item_id)}",
                TODO_STATUS_NEEDS_ACTION,
                self._purchase_description(item_id),
            )
        # 2. 本轮恢复正常：已存在的对应待办 → completed（不删，保留历史）
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
        # 3. 清理升级遗留的合并版待办（无后缀的旧格式）
        self._todos.pop(self._auto_uid(TODO_KIND_PURCHASE), None)

    def _alert_text(self, style: str) -> str:
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

    def _compute_triggered(self) -> TriggeredSet:
        """更换触发集合评估：沿用现有 triggered_entities 评估逻辑（零变化数据源）。"""
        return TriggeredSet(
            kind="replace",
            members=tuple(self._eval_triggered_entities()),
        )

    def _eval_triggered_entities(self) -> list[str]:
        """触发实体评估（评估阶段唯一调用；下游只读 _current_triggered）。"""
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

    @property
    def replace_status(self) -> str:
        """更换状态：直接从 TriggeredSet 取整体状态。

        第一轮刷新尚未完成（setup 期极短窗口 / 测试构造后不 refresh）时，
        兜底实时评估保证兼容。
        """
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
        # 以上 triggered_entities() 已收敛到单一事实源，不再独立评估

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
            display = state_name or snapshot.get("device_name")
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
    def _sync_todos(self,
        newly_triggered: tuple[str, ...],
        newly_resolved: tuple[str, ...],
    ) -> None:
        """每个触发实体各生成一条「更换」待办（差集驱动）。

        用差集直接定位：
        - newly_triggered：本轮新增越过阈值的 entity_id → needs_action
        - newly_resolved：本轮恢复正常的 entity_id → completed
        现有仍低库存的项保持 needs_action（无需再循环遍历 triggered）。
        「实体解绑删除遗留待办」仍然保留（独立于差集的 bound 集合扫描）。
        """
        bound = set(self.source_entities)
        replace_prefix = self._auto_uid(TODO_KIND_REPLACE) + "_"
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
    def async_subscribe(self) -> None:
        """订阅绑定实体状态变化，即时刷新（手动改值即时检测跳变通知）。"""
        entities = self.source_entities
        if entities:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(entities), self._on_state_change
            )

    @callback
    def _on_state_change(self, event: Any) -> None:
        """绑定实体状态变化 → 请求刷新（触发差集管线：评估 → diff → 待办/通知）。"""
        self.hass.async_create_task(self.async_request_refresh())

    def triggered_pairs(self) -> list[tuple[str, str, str]]:
        """越过阈值实体的 (显示名, 状态值, 单位)；显示名取快照或实体名。

        遍历对象：triggered_entities()/triggered_values() 已收敛到
        单一事实源 _current_triggered，不再独立评估。
        """
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

    def _alert_text(self, style: str) -> str:
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
