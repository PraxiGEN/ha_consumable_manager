"""耗材管理器 协调器包（按功能拆分：base / stock / type / factory）。"""
from __future__ import annotations

from .base import (
    BaseCoordinator,
    ConsumableManagerData,
    REPLACE_STATES,
    STOCK_STATES,
    TriggeredSet,
    _to_float,
    evaluate_threshold,
)
from ..const import (
    STATE_LOW_STOCK,
    STATE_OK,
    STATE_REPLACE_NEEDED,
    TODO_STATUS_COMPLETED,
    TODO_STATUS_NEEDS_ACTION,
)
from .factory import build_coordinator
from .stock import (
    StockCoordinator,
    _find_stock_coordinator,
)
from .type import ConsumableTypeCoordinator

__all__ = [
    "BaseCoordinator",
    "StockCoordinator",
    "ConsumableTypeCoordinator",
    "TriggeredSet",
    "build_coordinator",
    "_find_stock_coordinator",
    "_to_float",
    "evaluate_threshold",
    "ConsumableManagerData",
    "STOCK_STATES",
    "REPLACE_STATES",
    "STATE_OK",
    "STATE_LOW_STOCK",
    "STATE_REPLACE_NEEDED",
    "TODO_STATUS_NEEDS_ACTION",
    "TODO_STATUS_COMPLETED",
]