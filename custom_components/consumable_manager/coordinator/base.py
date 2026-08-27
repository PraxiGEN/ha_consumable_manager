"""协调器基类与共享原语（叶子模块，零兄弟导入）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ..const import (
    DOMAIN, LOGGER, CONF_ENTRY_TYPE, ENTRY_SORT_PREFIXES, CONF_LAST_TRIGGERED_SIG,
    CONF_NOTIFY_MODE, CONF_NOTIFY_STYLE, NOTIFY_MODE_REALTIME, NOTIFY_MODE_SCHEDULED,
    NOTIFY_STYLE_HUMAN, TODO_STATUS_COMPLETED, STATE_OK,
    STATE_LOW_STOCK, STATE_REPLACE_NEEDED, THRESHOLD_TYPE_LIFETIME_PERCENT,
    THRESHOLD_TYPE_NUMERIC, TIME_UOM_TO_HOURS, OPERATOR_EQUAL, OPERATOR_GREATER_THAN,
    OPERATOR_LESS_THAN,
)
from ..user_library import async_load_library
from ..notifications import (
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
    # 单位换算：时间类换算到小时（内部标准单位），剩余寿命% / 数值类不换算。
    # 读数侧 (_read_values) 同样经 TIME_UOM_TO_HOURS 换算到小时，保证阈值与读数
    # 始终在同一个内部单位（小时）上比较，避免口径不一致导致误触发 / 漏触发。
    if unit is not None and threshold_type not in (
        THRESHOLD_TYPE_LIFETIME_PERCENT, THRESHOLD_TYPE_NUMERIC
    ):
        threshold = threshold * TIME_UOM_TO_HOURS.get(unit, 1.0)
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
        """本条目当前应生成的实体键集合。"""
        return ()

    @callback
    def _write_options(self, options: dict[str, Any]) -> None:
        """写回条目配置（持久化，重启不丢）。"""
        self.hass.config_entries.async_update_entry(
            self._entry, options=options
        )

    @callback
    def async_subscribe(self) -> Callable[[], None] | None:
        """建立运行时订阅（子类按需重写；库存/通知条目不订阅）。"""
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
            await self._async_send_alert()
        if not new:
            self.alert_pending = False

    def _persist_alert_baseline(
        self, new_triggered: TriggeredSet, force: bool = False
    ) -> None:
        """持久化触发集合签名（供 reload 后恢复基线）。"""
        if not self._baseline_established and not force:
            return
        sig = new_triggered.signature()
        if self._entry.options.get(CONF_LAST_TRIGGERED_SIG) == sig:
            return
        options = dict(self._entry.options)
        options[CONF_LAST_TRIGGERED_SIG] = sig
        self._write_options(options)

    def sync_alert_baseline(self) -> None:
        """配置变更（绑定/解绑/编辑分组）后调用：把当前触发集合作为新基线。"""
        self._prev_triggered = self._compute_triggered()
        self._persist_alert_baseline(self._prev_triggered, force=True)

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

@dataclass
class ConsumableManagerData:
    """每个配置条目的运行时数据（存于 entry.runtime_data）。"""
    coordinator: BaseCoordinator
    # 条目建立时的实体集合签名，用于判断配置变更是否需要重载
    entity_signature: tuple[str, ...] = ()