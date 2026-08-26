"""库存条目协调器。"""
from __future__ import annotations

from typing import Any, Literal

from homeassistant.core import HomeAssistant, callback

from .base import BaseCoordinator, ConsumableManagerData, TriggeredSet
from ..const import (
    CONF_ENTRY_TYPE, CONF_ITEM_ID, CONF_ITEM_NAME, CONF_ITEM_TYPE, CONF_MODEL, CONF_QUANTITY,
    CONF_STOCK_ITEMS, CONF_STOCK_THRESHOLD, CONF_UNIT, DOMAIN, ENTRY_TYPE_STOCK,
    NOTIFY_STYLE_VALUE, NOTIFY_TEXT_DESC_THRESHOLD, NOTIFY_TEXT_LOW_STOCK,
    STATE_LOW_STOCK, STATE_OK, TODO_KIND_PURCHASE, TODO_STATUS_COMPLETED,
    TODO_STATUS_NEEDS_ACTION,
)

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