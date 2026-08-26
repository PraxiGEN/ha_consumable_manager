"""协调器工厂：按条目类型构建对应协调器。"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .base import BaseCoordinator
from ..const import (
    CONF_ENTRY_TYPE, ENTRY_TYPE_NOTIFICATION, ENTRY_TYPE_STOCK, UPDATE_INTERVAL_SECONDS,
)
from ..library import Library, TypeMeta
from .stock import StockCoordinator
from .type import ConsumableTypeCoordinator

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