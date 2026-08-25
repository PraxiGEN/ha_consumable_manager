"""耗材管理器 传感器平台。"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ConsumableManagerConfigEntry
from .const import (
    CONF_GROUP_ID,
    CONF_GROUP_NAME,
    CONF_ITEM_ID,
    CONF_ITEM_NAME,
    CONF_ITEM_TYPE,
    CONF_UNIT,
)
from .coordinator import (
    REPLACE_STATES,
    STATE_LOW_STOCK,
    STATE_OK,
    STATE_REPLACE_NEEDED,
    STOCK_STATES,
    ConsumableTypeCoordinator,
    StockCoordinator,
)
from .user_library import async_load_library

_DEFAULT_ICON = "mdi:package-variant"
_ICON_SHORTAGE = "mdi:package-variant-closed-remove"
_ICON_BY_STATE: dict[str, str] = {
    STATE_OK: "mdi:check-circle-outline",
    STATE_LOW_STOCK: "mdi:archive-alert-outline",
    STATE_REPLACE_NEEDED: "mdi:alert-decagram-outline",
}

# ---- 静态实体描述符 ----
STOCK_STATUS_DESCRIPTION = SensorEntityDescription(
    key="stock_status",
    translation_key="stock_status",
    device_class=SensorDeviceClass.ENUM,
    options=list(STOCK_STATES),
    entity_category=EntityCategory.DIAGNOSTIC,
)
REPLACE_STATUS_DESCRIPTION = SensorEntityDescription(
    key="replace_status",
    translation_key="replace_status",
    device_class=SensorDeviceClass.ENUM,
    options=list(REPLACE_STATES),
    entity_category=EntityCategory.DIAGNOSTIC,
)

def build_item_description(item: dict) -> SensorEntityDescription:
    """按用户配置的库存项动态构建描述符（名称与单位均来自用户输入）。"""
    return SensorEntityDescription(
        key=item[CONF_ITEM_ID],
        name=item.get(CONF_ITEM_NAME) or item[CONF_ITEM_ID],
        native_unit_of_measurement=item.get(CONF_UNIT) or None,
        state_class=SensorStateClass.MEASUREMENT,
    )

class StockItemSensor(CoordinatorEntity[StockCoordinator], SensorEntity):
    """库存实体：主状态 = 某个库存项的数量。"""

    _attr_has_entity_name = True
    _attr_translation_key = "stock_item"

    def __init__(
        self,
        coordinator: StockCoordinator,
        description: SensorEntityDescription,
        item_type: str | None,
        type_icons: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._item_id = description.key
        self._item_type = item_type
        # 类型 → 图标（来自库类型元数据，动态类型不再硬编码）
        self._type_icons = type_icons or {}
        self._attr_unique_id = f"{coordinator.entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def item_id(self) -> str:
        """该实体对应的库存项 id（供 adjust_stock 服务定位）。"""
        return self._item_id

    @property
    def native_value(self) -> int:
        return self.coordinator.quantity(self._item_id)

    @property
    def icon(self) -> str:
        """欠货时切换警示图标，否则按关联耗材类型取图标（库元数据）。"""
        if self.coordinator.quantity(self._item_id) < 0:
            return _ICON_SHORTAGE
        return self._type_icons.get(self._item_type or "", _DEFAULT_ICON)

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.item_attributes(self._item_id)


class StockStatusSensor(CoordinatorEntity[StockCoordinator], SensorEntity):
    """库存汇总实体：主状态 = 正常 / 库存不足（枚举）。"""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: StockCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> str:
        return self.coordinator.stock_status

    @property
    def icon(self) -> str:
        return _ICON_BY_STATE[self.coordinator.stock_status]

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.status_attributes()


class ReplaceStatusSensor(
    CoordinatorEntity[ConsumableTypeCoordinator], SensorEntity
):
    """更换状态实体：主状态 = 正常 / 需要更换（枚举），按分组独立生成。"""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ConsumableTypeCoordinator,
        description: SensorEntityDescription,
        group: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._group = group
        group_id = group.get(CONF_GROUP_ID, "default")
        self._attr_unique_id = f"{coordinator.entry_id}_grp_{group_id}"
        self._attr_device_info = coordinator.device_info
        # 实体名 = 分组名（设备为该条目类型），各组互不重名
        self._attr_name = group.get(CONF_GROUP_NAME) or "默认"

    @property
    def native_value(self) -> str:
        return self.coordinator.group_status(self._group)

    @property
    def icon(self) -> str:
        return _ICON_BY_STATE[self.coordinator.group_status(self._group)]

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.group_attributes(self._group)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """按条目类型生成传感器实体（未配置时不生成任何实体）。"""
    coordinator = entry.runtime_data.coordinator

    if isinstance(coordinator, StockCoordinator):
        items = coordinator.items
        if not items:
            return
        # 类型图标映射（库类型元数据驱动，含用户库覆盖）
        library = await async_load_library(hass)
        type_icons = {
            key: meta.icon
            for key in library.types
            if (meta := library.type_meta(key)) is not None
        }
        entities: list[SensorEntity] = [
            StockItemSensor(
                coordinator,
                build_item_description(item),
                item.get(CONF_ITEM_TYPE),
                type_icons,
            )
            for item in items
        ]
        entities.append(
            StockStatusSensor(coordinator, STOCK_STATUS_DESCRIPTION)
        )
        async_add_entities(entities)
        return

    # 通知条目（BaseCoordinator，无实体）与其余未配置条目不生成实体
    if not isinstance(coordinator, ConsumableTypeCoordinator):
        return
    groups = coordinator.groups
    if not groups:
        return
    async_add_entities(
        [
            ReplaceStatusSensor(coordinator, REPLACE_STATUS_DESCRIPTION, group)
            for group in groups
        ]
    )