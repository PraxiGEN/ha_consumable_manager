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

GROUP_DATA_DESCRIPTION = SensorEntityDescription(
    key="group_entity_data",
    translation_key="group_entity_data",
    state_class=SensorStateClass.MEASUREMENT,
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
        # 继承耗材类型图标，使同类型条目下实体视觉统一归属
        return self.coordinator.type_icon

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.group_attributes(self._group)

class GroupDataSensor(
    CoordinatorEntity[ConsumableTypeCoordinator], SensorEntity
):
    """分组数据传感器：主状态 = 组内实时值最小值，属性暴露成员明细。"""

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
        group_name = group.get(CONF_GROUP_NAME) or "默认"
        self._attr_unique_id = f"{coordinator.entry_id}_grpdata_{group_id}"
        self._attr_device_info = coordinator.device_info
        # 实体名 = 分组名 + 「数据」后缀，避免与诊断实体（仅分组名）重名
        self._attr_name = f"{group_name}数据"
        # 主状态 = 组内实时值最小值（纯数值，不带单位，便于自动化比较）

    @property
    def native_value(self) -> float | None:
        return self.coordinator.group_min_value(self._group)

    @property
    def icon(self) -> str:
        # 继承耗材类型图标，使同类型条目下实体视觉统一归属
        return self.coordinator.type_icon

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.group_member_data(self._group)
        # 拍平为「已绑定实体数据」：每条组装成「实体名称 + 实体返回值」，
        # 直接取实体的原始 state，不手动追加单位（实体返回什么就显示什么）
        members = (
            data.get("normal_entities", [])
            + data.get("triggered_entities", [])
        )
        bound: list[str] = []
        for m in members:
            name = m["name"]
            entity_id = m["entity_id"]
            state = self.coordinator.hass.states.get(entity_id)
            raw = (
                state.state
                if state is not None
                and state.state not in (None, "unknown", "unavailable")
                else None
            )
            bound.append(
                f"{name} {raw}" if raw is not None else f"{name} 未知"
            )
        return {
            "group": data.get("group"),
            "consumable_type": data.get("consumable_type"),
            "bound_entity_data": bound,
        }

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
    entities: list[SensorEntity] = []
    for group in groups:
        entities.append(
            ReplaceStatusSensor(coordinator, REPLACE_STATUS_DESCRIPTION, group)
        )
        # 分组数据传感器仅对有绑定实体的非自定义分组生成
        if not coordinator._group_is_custom(group):
            entities.append(
                GroupDataSensor(coordinator, GROUP_DATA_DESCRIPTION, group)
            )
    async_add_entities(entities)