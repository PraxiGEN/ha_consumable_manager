"""耗材管理器 待办平台。"""
from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ConsumableManagerConfigEntry
from .coordinator import TODO_STATUS_NEEDS_ACTION, BaseCoordinator

class ConsumableTodoListEntity(
    CoordinatorEntity[BaseCoordinator], TodoListEntity
):
    """耗材待办列表实体（数据来自协调器）。"""
    _attr_has_entity_name = True
    _attr_translation_key = "todo"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, coordinator: BaseCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_todo"
        self._attr_device_info = coordinator.device_info

    @property
    def todo_items(self) -> list[TodoItem]:
        """待办列表（由协调器运行时数据转换）。"""
        return [
            TodoItem(
                uid=item["uid"],
                summary=item["summary"],
                status=TodoItemStatus(item["status"]),
                due=item.get("due"),
                description=item.get("description"),
                completed=item.get("completed"),
            )
            for item in self.coordinator.todo_dicts()
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """创建待办（uid 为空时由协调器分配）。"""
        self.coordinator.async_upsert_todo(
            uid=item.uid,
            summary=item.summary,
            status=item.status.value
            if item.status
            else TODO_STATUS_NEEDS_ACTION,
            due=item.due,
            description=item.description,
        )
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """更新待办；勾选「更换」待办即视为已更换。"""
        if item.uid is None:
            return
        old_status = self.coordinator._todos.get(item.uid, {}).get("status")
        new_status = (
            item.status.value if item.status else TODO_STATUS_NEEDS_ACTION
        )
        self.coordinator.async_upsert_todo(
            uid=item.uid,
            summary=item.summary,
            status=new_status,
            due=item.due,
            description=item.description,
        )
        # 勾选「更换」待办 = 已更换（记录时间 + 联动扣减库存）
        self.coordinator.async_on_todo_completed(
            item.uid, old_status, new_status
        )
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """删除待办。"""
        self.coordinator.async_delete_todos(set(uids))
        await self.coordinator.async_request_refresh()

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置待办事项平台（条目未配置时不生成实体）。"""
    coordinator = entry.runtime_data.coordinator
    if not coordinator.entity_signature:
        return
    async_add_entities([ConsumableTodoListEntity(coordinator)])