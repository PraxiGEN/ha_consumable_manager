"""耗材管理器 配置流平台。"""
from __future__ import annotations

import datetime
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector, translation

from .const import (
    DOMAIN, ADD_METHOD_CONSUMABLE, ADD_METHOD_CUSTOM_CONSUMABLE, CONF_ADD_METHOD,
    CONF_CONSUMABLE_ID, CONF_ENTITY_REGEX, CONF_ENTRY_TYPE,
    CONF_ITEM_ID, CONF_ITEM_NAME, CONF_ITEM_TYPE, CONF_MODEL,
    CONF_NOTIFICATION, CONF_NOTIFY_CUSTOMIZE, CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_MODE, CONF_NOTIFY_SCHEDULE_TIME, CONF_NOTIFY_STYLE,
    CONF_NOTIFY_SYSTEM, NOTIFY_MODE_REALTIME, NOTIFY_MODE_SCHEDULED,
    NOTIFY_MODES,
    NOTIFY_STYLE_HUMAN, NOTIFY_STYLES, CONF_QUANTITY, CONF_REMOVE_ITEMS, CONF_SELECTED_ITEM,
    CONF_SOURCE_ENTITIES, CONF_STOCK_ITEMS, CONF_STOCK_THRESHOLD,
    CONF_BINDING_GROUPS, CONF_ADDED_AT, CONF_GROUP_ID, CONF_GROUP_KIND,
    CONF_GROUP_NAME, GROUP_KIND_BINDING, GROUP_KIND_CUSTOM,
    CONF_SELECTED_GROUP, CONF_REMOVE_GROUPS,
    CONF_THRESHOLD, CONF_THRESHOLD_OPERATOR, CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT, CONF_UNIT, DEFAULT_THRESHOLD,
    DEFAULT_THRESHOLD_TYPE, DEFAULT_THRESHOLD_UNIT, ENTRY_SORT_PREFIXES,
    ENTRY_TYPE_CUSTOM, ENTRY_TYPE_NOTIFICATION, ENTRY_TYPE_STOCK,
    CONF_TYPE_KEY, CONF_TYPE_NAME_ZH, CONF_TYPE_ICON,CONF_TYPE_THRESHOLD_TYPE,
    CONF_TYPE_THRESHOLD, CONF_TYPE_THRESHOLD_UNIT, OPERATORS,
    THRESHOLD_DEFAULT_OPERATOR, THRESHOLD_TYPES, THRESHOLD_UNIT_OPTIONS,
    THRESHOLD_TYPE_USED_TIME, OPERATOR_GREATER_THAN, UNIT_DAYS,
)
from .library import Consumable, ID_PATTERN, Library
from .user_library import (
    async_load_library,
    async_write_user_consumable,
    async_write_user_type,
)

async def _load_library(hass: HomeAssistant) -> Library:
    """在 executor 中加载内置库 + 用户库合并结果。"""
    return await async_load_library(hass)

def _norm_time(value: Any) -> str:
    """时间选择器返回的 datetime.time 或 "HH:MM[:SS]" 规范化为 "HH:MM"。"""
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    text = str(value or "")
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text

def _build_entry_type_options(library: Library,
    translations: dict[str, str],
    language: str,
) -> list[dict[str, str]]:
    """添加界面「类型」下拉选项：通知渠道设置 + 库存 + 库类型 + 自定义。"""
    prefix = f"component.{DOMAIN}.selector.entry_type.options."
    options = [
        {
            "label": translations.get(
                f"{prefix}{ENTRY_TYPE_NOTIFICATION}", ENTRY_TYPE_NOTIFICATION
            ),
            "value": ENTRY_TYPE_NOTIFICATION,
        },
        {
            "label": translations.get(
                f"{prefix}{ENTRY_TYPE_STOCK}", ENTRY_TYPE_STOCK
            ),
            "value": ENTRY_TYPE_STOCK,
        },
    ]
    options.extend(
        {
            "label": meta.display_name(language),
            "value": key,
        }
        for key in library.types
        if (meta := library.type_meta(key)) is not None
    )
    # 自定义类型（向导二级界面，写入用户库 types 段）放在最末
    options.append(
        {
            "label": translations.get(
                f"{prefix}{ENTRY_TYPE_CUSTOM}", ENTRY_TYPE_CUSTOM
            ),
            "value": ENTRY_TYPE_CUSTOM,
        }
    )
    return options

def build_source_snapshots(hass: HomeAssistant,
    entity_ids: list[str],
) -> list[dict[str, Any]]:
    """反查实体注册表 + 设备注册表，构建绑定实体快照。"""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    snapshots: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        reg_entry = ent_reg.async_get(entity_id)
        device = None
        if reg_entry and reg_entry.device_id:
            device = dev_reg.async_get(reg_entry.device_id)
        snapshots.append(
            {
                "entity_id": entity_id,
                "device_name": device.name if device else None,
                "device_model": device.model if device else None,
                "manufacturer": device.manufacturer if device else None,
            }
        )
    return snapshots

async def async_entry_type_title(hass: HomeAssistant,
    entry_type: str,
    library: Library | None = None,
) -> str:
    """按当前语言取条目标题。"""
    if library is not None and (
        meta := library.type_meta(entry_type)
    ) is not None:
        title = meta.display_name(hass.config.language)
    else:
        translations = await translation.async_get_translations(
            hass,
            hass.config.language,
            "selector",
            integrations=[DOMAIN],
        )
        title = translations.get(
            f"component.{DOMAIN}.selector.entry_type.options.{entry_type}",
            entry_type,
        )
    return f"{ENTRY_SORT_PREFIXES.get(entry_type, '')}{title}"

class ConsumableManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """添加界面：只选类型，每个类型仅允许一个条目。"""

    VERSION = 1

    async def async_step_user(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """第一步：选择类型（库存 / 某耗材类型），类型清单来自库。"""
        library = await _load_library(self.hass)
        if user_input is not None:
            entry_type = user_input[CONF_ENTRY_TYPE]
            if entry_type == ENTRY_TYPE_CUSTOM:
                return await self.async_step_custom_type()
            # 每个类型只能添加一次
            await self.async_set_unique_id(entry_type)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=await async_entry_type_title(
                    self.hass, entry_type, library
                ),
                data={CONF_ENTRY_TYPE: entry_type},
            )

        # 类型下拉动态构建：库存 + 库类型（显示名来自库元数据）
        language = self.hass.config.language
        translations = await translation.async_get_translations(
            self.hass, language, "selector", integrations=[DOMAIN]
        )
        options = _build_entry_type_options(library, translations, language)

        schema = vol.Schema(
            {
                vol.Required(CONF_ENTRY_TYPE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_custom_type(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """自定义类型向导：填类型键 + 名称 + 图标 + 默认阈值，写入用户库后建条目。"""
        errors: dict[str, str] = {}
        language = self.hass.config.language
        translations = await translation.async_get_translations(
            self.hass, language, "selector", integrations=[DOMAIN]
        )
        library = await _load_library(self.hass)

        if user_input is not None:
            key = str(user_input.get(CONF_TYPE_KEY, "")).strip().lower()
            name = str(user_input.get(CONF_TYPE_NAME_ZH, "")).strip()
            icon = str(user_input.get(CONF_TYPE_ICON, "")).strip()
            threshold_type = user_input.get(CONF_TYPE_THRESHOLD_TYPE)
            threshold = user_input.get(CONF_TYPE_THRESHOLD)
            threshold_unit = user_input.get(CONF_TYPE_THRESHOLD_UNIT)

            # 类型键：格式 + 唯一性（新建语义，禁止覆盖内置/已有类型）
            if not key:
                errors["type_key"] = "required"
            elif not ID_PATTERN.match(key):
                errors["type_key"] = "invalid_key"
            elif key in library.types:
                errors["type_key"] = "key_exists"

            if not name:
                errors["type_name_zh"] = "required"

            if not icon:
                icon = "mdi:package-variant"

            threshold_val: float | None = None
            try:
                threshold_val = float(threshold)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                errors["type_threshold"] = "invalid"

            if not errors:
                meta = {
                    "name": name,
                    "icon": icon,
                    "default_threshold_type": threshold_type,
                    "default_threshold": threshold_val,
                    "default_threshold_unit": threshold_unit,
                }
                await async_write_user_type(self.hass, key, meta)
                await self.async_set_unique_id(key)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={CONF_ENTRY_TYPE: key},
                )

        # 阈值类型 / 单位下拉选项（复用 selector 翻译键）
        tprefix = f"component.{DOMAIN}.selector.threshold_type.options."
        uprefix = f"component.{DOMAIN}.selector.threshold_unit.options."
        threshold_type_options = [
            {"label": translations.get(f"{tprefix}{t}", t), "value": t}
            for t in THRESHOLD_TYPES
        ]
        threshold_unit_options = [
            {"label": translations.get(f"{uprefix}{u}", u), "value": u}
            for u in THRESHOLD_UNIT_OPTIONS
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_TYPE_KEY): selector.TextSelector(
                    selector.TextSelectorConfig()
                ),
                vol.Required(CONF_TYPE_NAME_ZH): selector.TextSelector(
                    selector.TextSelectorConfig()
                ),
                vol.Optional(
                    CONF_TYPE_ICON, default="mdi:package-variant"
                ): selector.IconSelector(selector.IconSelectorConfig()),
                vol.Required(
                    CONF_TYPE_THRESHOLD_TYPE, default=DEFAULT_THRESHOLD_TYPE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=threshold_type_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_TYPE_THRESHOLD, default=DEFAULT_THRESHOLD
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_TYPE_THRESHOLD_UNIT, default=DEFAULT_THRESHOLD_UNIT
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=threshold_unit_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="custom_type",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry,) -> ConsumableManagerOptionsFlow:
        """配置界面入口。"""
        return ConsumableManagerOptionsFlow()

class ConsumableManagerOptionsFlow(OptionsFlow):
    """配置界面：按条目类型分流。"""

    _item_id: str | None = None
    _item_type: str | None = None
    _library_cache: Library | None = None

    @property
    def _is_stock_entry(self) -> bool:
        return (
            self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_STOCK
        )

    @property
    def _is_notification_entry(self) -> bool:
        """通知条目：仅承载全局通知配置，无菜单直接进表单。"""
        return (
            self.config_entry.data.get(CONF_ENTRY_TYPE)
            == ENTRY_TYPE_NOTIFICATION
        )

    async def _library(self) -> Library:
        """加载合并库（executor 中读盘，流程内缓存）。"""
        if self._library_cache is None:
            self._library_cache = await async_load_library(self.hass)
        return self._library_cache

    async def _type_labels(self) -> dict[str, str]:
        """耗材类型标签：库类型走元数据显示名（类型必选，无"未关联"占位）。"""
        library = await self._library()
        language = self.hass.config.language
        return {
            key: meta.display_name(language)
            for key in library.types
            if (meta := library.type_meta(key)) is not None
        }

    def _type_dropdown_options( self, labels: dict[str, str] ) -> list[dict[str, str]]:
        """关联耗材类型下拉选项（仅库类型，类型必须关联）。"""
        return [
            {"label": label, "value": key}
            for key, label in labels.items()
        ]

    def _consumable_options(self,
        results: tuple[Consumable, ...],
        type_labels: dict[str, str],
        language: str = "",
    ) -> list[dict[str, str]]:
        """耗材下拉选项（label = 类型名 › 耗材显示名）。"""
        options: list[dict[str, str]] = []
        for consumable in results:
            options.append(
                {
                    "label": f"{type_labels.get(consumable.cons_type, consumable.cons_type)} › "
                    f"{consumable.display_name(language)}",
                    "value": consumable.id,
                }
            )
        return options

    @property
    def _items(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_STOCK_ITEMS, []))

    def _save(self, options: dict[str, Any]) -> ConfigFlowResult:
        """写入条目配置并结束流程。"""
        return self.async_create_entry(title="", data=options)

    def _item_options(self) -> list[dict[str, str]]:
        """库存项下拉选项（label = 自定义名称，value = 项 id）。"""
        return [
            {
                "label": item.get(CONF_ITEM_NAME, item[CONF_ITEM_ID]),
                "value": item[CONF_ITEM_ID],
            }
            for item in self._items
        ]

    async def async_step_init(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """入口：通知条目直达表单；其余条目显示菜单（均含「通知」项）。"""
        if self._is_notification_entry:
            return await self.async_step_notification(user_input)

        if not self._is_stock_entry:
            # 耗材类型条目：像库存一样，1级菜单直接列出分组管理各项
            menu_options = ["add_group"]
            if self._current_groups():
                menu_options += ["select_group", "remove_groups"]
            menu_options += ["threshold", "notification"]
            return self.async_show_menu(
                step_id="init", menu_options=menu_options
            )

        menu_options = ["add_item"]
        if self._items:
            menu_options += ["select_item", "remove_items"]
        # 通知渠道设置固定在菜单最末（与类型条目一致）
        menu_options.append("notification")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    # ---- 耗材类型条目：绑定分组（多分组 → 多诊断实体）----
    def _current_groups(self) -> list[dict[str, Any]]:
        """当前分组列表（已存 binding_groups，或扁平 source_entities 合成默认组）。"""
        stored = self.config_entry.options.get(CONF_BINDING_GROUPS)
        if stored:
            return [dict(g) for g in stored]
        flat = self.config_entry.options.get(CONF_SOURCE_ENTITIES, [])
        if flat:
            return [{
                CONF_GROUP_ID: "default",
                CONF_GROUP_NAME: self.config_entry.title,
                CONF_SOURCE_ENTITIES: list(flat),
            }]
        return []

    def _save_groups(self, groups: list[dict[str, Any]]) -> ConfigFlowResult:
        """写回分组列表（规范化到 binding_groups，清理旧扁平键）。"""
        options = dict(self.config_entry.options)
        if groups:
            options[CONF_BINDING_GROUPS] = groups
            options.pop(CONF_SOURCE_ENTITIES, None)
        else:
            options.pop(CONF_BINDING_GROUPS, None)
            options.pop(CONF_SOURCE_ENTITIES, None)
        return self._save(options)

    def _group_options(self) -> list[dict[str, str]]:
        """分组下拉选项（label = 分组名，value = group_id）。"""
        return [
            {
                "label": g.get(CONF_GROUP_NAME, "未命名"),
                "value": g.get(CONF_GROUP_ID, ""),
            }
            for g in self._current_groups()
        ]

    async def async_step_add_group(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """新建分组（菜单项）：先选分组类别，再进入对应表单。

        - 绑定实体：常规分组表单（绑定传感器 / 群组）
        - 自定义耗材实体：自建数据（名称 + 添加时间 + 已使用时长阈值）
        """
        if user_input is not None:
            self._group_edit_idx = None
            kind = user_input.get(CONF_GROUP_KIND, GROUP_KIND_BINDING)
            if kind == GROUP_KIND_CUSTOM:
                return await self.async_step_custom_entity()
            return await self.async_step_group()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GROUP_KIND, default=GROUP_KIND_BINDING
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {
                                "label": "绑定实体",
                                "value": GROUP_KIND_BINDING,
                            },
                            {
                                "label": "自定义耗材实体",
                                "value": GROUP_KIND_CUSTOM,
                            },
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="add_group", data_schema=schema)

    async def async_step_select_group(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """修改分组：先选一个分组，再进入对应编辑表单（仿库存 select_item）。"""
        if user_input is not None:
            gid = user_input[CONF_SELECTED_GROUP]
            for i, g in enumerate(self._current_groups()):
                if g.get(CONF_GROUP_ID) == gid:
                    self._group_edit_idx = i
                    break
            if g.get(CONF_GROUP_KIND) == GROUP_KIND_CUSTOM:
                return await self.async_step_custom_entity()
            return await self.async_step_group()

        schema = vol.Schema(
            {
                vol.Required(CONF_SELECTED_GROUP): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._group_options(),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="select_group", data_schema=schema
        )

    async def async_step_remove_groups(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """删除分组（多选，仿库存 remove_items）。"""
        if user_input is not None:
            removing = set(user_input.get(CONF_REMOVE_GROUPS, []))
            groups = [
                g for g in self._current_groups()
                if g.get(CONF_GROUP_ID) not in removing
            ]
            return self._save_groups(groups)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REMOVE_GROUPS, default=[]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._group_options(),
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="remove_groups", data_schema=schema
        )

    async def async_step_group(self,
        user_input: dict[str, Any] | None = None,
        idx: int | None = None,
    ) -> ConfigFlowResult:
        """新增/编辑单个分组：名称 + 实体 + 可选阈值覆盖。"""
        errors: dict[str, str] = {}
        # HA 数据流始终以 user_input 作为第一个位置参数传入；
        # 编辑态的索引通过关键字或实例属性跨「展示表单 → 提交表单」传递。
        if idx is not None:
            self._group_edit_idx = idx
        groups = self._current_groups()
        edit_idx = getattr(self, "_group_edit_idx", None)
        editing = edit_idx is not None and 0 <= edit_idx < len(groups)
        group = groups[edit_idx] if editing else {}

        if user_input is not None:
            name = str(user_input.get(CONF_GROUP_NAME) or "").strip()
            if not name:
                errors["name"] = "required"
            else:
                manual = list(user_input.get(CONF_SOURCE_ENTITIES, []))
                regex = str(user_input.get(CONF_ENTITY_REGEX, "") or "")
                new_group: dict[str, Any] = {
                    CONF_GROUP_ID: group.get(CONF_GROUP_ID) or uuid4().hex[:8],
                    CONF_GROUP_NAME: name,
                    CONF_GROUP_KIND: GROUP_KIND_BINDING,
                    CONF_SOURCE_ENTITIES: build_source_snapshots(
                        self.hass, sorted(set(manual))
                    ),
                    # 正则仅存规则本身，运行时由协调器动态匹配（不再提交时合并）
                    CONF_ENTITY_REGEX: regex,
                }
                if user_input.get("override_threshold"):
                    new_group[CONF_THRESHOLD_TYPE] = user_input[CONF_THRESHOLD_TYPE]
                    new_group[CONF_THRESHOLD] = user_input[CONF_THRESHOLD]
                    new_group[CONF_THRESHOLD_UNIT] = user_input[CONF_THRESHOLD_UNIT]
                    new_group[CONF_THRESHOLD_OPERATOR] = user_input[
                        CONF_THRESHOLD_OPERATOR
                    ]
                if editing:
                    groups[self._group_edit_idx] = new_group
                else:
                    groups.append(new_group)
                return self._save_groups(groups)

        # 预填值
        default_name = group.get(CONF_GROUP_NAME, "")
        default_entities = [
            s["entity_id"]
            for s in group.get(CONF_SOURCE_ENTITIES, [])
            if s.get("entity_id")
        ]
        override_on = any(
            k in group
            for k in (CONF_THRESHOLD, CONF_THRESHOLD_TYPE, CONF_THRESHOLD_UNIT,
                      CONF_THRESHOLD_OPERATOR)
        )
        # 阈值默认值兜底链：分组覆盖 → 库类型元数据 → 通用兜底
        library = await self._library()
        meta = library.type_meta(
            self.config_entry.data.get(CONF_ENTRY_TYPE, "")
        )
        d_type = meta.default_threshold_type if meta else DEFAULT_THRESHOLD_TYPE
        d_val = meta.default_threshold if meta else DEFAULT_THRESHOLD
        d_unit = meta.default_threshold_unit if meta else DEFAULT_THRESHOLD_UNIT
        t_type = group.get(CONF_THRESHOLD_TYPE, d_type)
        t_val = group.get(CONF_THRESHOLD, d_val)
        t_unit = group.get(CONF_THRESHOLD_UNIT, d_unit)
        t_op = group.get(
            CONF_THRESHOLD_OPERATOR, THRESHOLD_DEFAULT_OPERATOR[t_type]
        )

        threshold_type_options = [
            {"label": t, "value": t} for t in THRESHOLD_TYPES
        ]
        threshold_unit_options = [
            {"label": u, "value": u} for u in THRESHOLD_UNIT_OPTIONS
        ]
        operator_options = [
            {"label": o, "value": o} for o in OPERATORS
        ]

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GROUP_NAME, default=default_name
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Optional(
                    CONF_ENTITY_REGEX, default=group.get(CONF_ENTITY_REGEX, "")
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Optional(
                    CONF_SOURCE_ENTITIES, default=default_entities
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", multiple=True
                    )
                ),
                vol.Optional(
                    "override_threshold", default=override_on
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_THRESHOLD_TYPE, default=t_type
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=threshold_type_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_type",
                    )
                ),
                vol.Optional(
                    CONF_THRESHOLD, default=t_val
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, step="any", mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_THRESHOLD_UNIT, default=t_unit
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=threshold_unit_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_unit",
                    )
                ),
                vol.Optional(
                    CONF_THRESHOLD_OPERATOR, default=t_op
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=operator_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_operator",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="group",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_custom_entity(self,
        user_input: dict[str, Any] | None = None,
        idx: int | None = None,
    ) -> ConfigFlowResult:
        """自定义耗材实体：名称 + 添加时间 + 已使用时长阈值（自建数据 → 诊断实体）。"""
        errors: dict[str, str] = {}
        # HA 数据流始终以 user_input 作为第一个位置参数传入；
        # 编辑态的索引通过关键字或实例属性跨「展示表单 → 提交表单」传递。
        if idx is not None:
            self._group_edit_idx = idx
        groups = self._current_groups()
        edit_idx = getattr(self, "_group_edit_idx", None)
        editing = edit_idx is not None and 0 <= edit_idx < len(groups)
        group = groups[edit_idx] if editing else {}

        if user_input is not None:
            name = str(user_input.get(CONF_GROUP_NAME) or "").strip()
            added_at = user_input.get(CONF_ADDED_AT)
            if not name:
                errors["name"] = "required"
            elif not added_at:
                errors["added_at"] = "required"
            else:
                new_group: dict[str, Any] = {
                    CONF_GROUP_ID: group.get(CONF_GROUP_ID) or uuid4().hex[:8],
                    CONF_GROUP_NAME: name,
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: added_at,
                    # 自定义分组阈值恒为「已使用时长」，大于阈值即触发
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_USED_TIME,
                    CONF_THRESHOLD: user_input[CONF_THRESHOLD],
                    CONF_THRESHOLD_UNIT: user_input[CONF_THRESHOLD_UNIT],
                    CONF_THRESHOLD_OPERATOR: OPERATOR_GREATER_THAN,
                }
                if editing:
                    groups[self._group_edit_idx] = new_group
                else:
                    groups.append(new_group)
                return self._save_groups(groups)

        default_name = group.get(CONF_GROUP_NAME, self.config_entry.title)
        default_added_at = group.get(
            CONF_ADDED_AT, datetime.date.today().isoformat()
        )
        default_threshold = group.get(CONF_THRESHOLD, 180)
        default_unit = group.get(CONF_THRESHOLD_UNIT, UNIT_DAYS)
        threshold_unit_options = [
            {"label": u, "value": u} for u in THRESHOLD_UNIT_OPTIONS
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GROUP_NAME, default=default_name
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Required(
                    CONF_ADDED_AT, default=default_added_at
                ): selector.DateSelector(),
                vol.Required(
                    CONF_THRESHOLD, default=default_threshold
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, step="any", mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD_UNIT, default=default_unit
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=threshold_unit_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_unit",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="custom_entity",
            data_schema=schema,
            errors=errors,
        )

    def _threshold_schema(self,
        threshold_type: str,
        threshold: float,
        unit: str,
        operator: str,
    ) -> vol.Schema:
        """阈值配置表单（类型 + 阈值 + 单位 + 计算方式）。"""
        return vol.Schema(
            {
                vol.Required(
                    CONF_THRESHOLD_TYPE, default=threshold_type
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(THRESHOLD_TYPES),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_type",
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD, default=threshold
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        step="any",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD_UNIT, default=unit
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(THRESHOLD_UNIT_OPTIONS),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_unit",
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD_OPERATOR, default=operator
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(OPERATORS),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="threshold_operator",
                    )
                ),
            }
        )

    async def async_step_threshold(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """设置阈值（类型 + 阈值 + 单位 + 计算方式）并保存。"""
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_THRESHOLD_TYPE] = user_input[CONF_THRESHOLD_TYPE]
            options[CONF_THRESHOLD] = user_input[CONF_THRESHOLD]
            options[CONF_THRESHOLD_UNIT] = user_input[CONF_THRESHOLD_UNIT]
            options[CONF_THRESHOLD_OPERATOR] = user_input[
                CONF_THRESHOLD_OPERATOR
            ]
            return self._save(options)

        # 阈值默认值兜底链：已存配置 → 库类型元数据 → 通用兜底
        library = await self._library()
        meta = library.type_meta(
            self.config_entry.data.get(CONF_ENTRY_TYPE, "")
        )
        default_type = (
            meta.default_threshold_type if meta else DEFAULT_THRESHOLD_TYPE
        )
        default_value = (
            meta.default_threshold if meta else DEFAULT_THRESHOLD
        )
        default_unit = (
            meta.default_threshold_unit
            if meta
            else DEFAULT_THRESHOLD_UNIT
        )

        threshold_type = self.config_entry.options.get(
            CONF_THRESHOLD_TYPE, default_type
        )
        threshold = self.config_entry.options.get(
            CONF_THRESHOLD, default_value
        )
        unit = self.config_entry.options.get(
            CONF_THRESHOLD_UNIT, default_unit
        )
        operator = self.config_entry.options.get(
            CONF_THRESHOLD_OPERATOR,
            THRESHOLD_DEFAULT_OPERATOR[threshold_type],
        )
        return self.async_show_form(
            step_id="threshold",
            data_schema=self._threshold_schema(
                threshold_type, threshold, unit, operator
            ),
        )

    # ---- 通知配置（全局通知条目 + 条目级覆盖共用表单）----
    def _global_notification_section(self) -> dict[str, Any]:
        """全局通知条目的通知段（无则空字典，作表单预填默认值）。"""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION:
                section = entry.options.get(CONF_NOTIFICATION)
                if isinstance(section, dict):
                    return section
        return {}

    async def async_step_notification(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """通知配置表单：渠道多选 + 推送模式 + 消息样式。 """
        errors: dict[str, str] = {}
        current_section = self.config_entry.options.get(CONF_NOTIFICATION)
        has_override = isinstance(current_section, dict)
        # 预填默认：条目级段优先，无段时用全局段（展示当前生效值）
        defaults = (
            current_section
            if has_override
            else self._global_notification_section()
        )

        def _default(key: str, fallback: Any) -> Any:
            value = defaults.get(key)
            return value if value is not None else fallback

        if user_input is not None:
            customize = (
                bool(user_input.get(CONF_NOTIFY_CUSTOMIZE))
                if not self._is_notification_entry
                else True
            )
            system = bool(user_input.get(CONF_NOTIFY_SYSTEM))
            entities = list(user_input.get(CONF_NOTIFY_ENTITIES, []) or [])
            style = str(
                user_input.get(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN)
            )
            mode = str(
                user_input.get(CONF_NOTIFY_MODE, NOTIFY_MODE_REALTIME)
            )
            # 时间字段可留空（vol.Any(None, …)）；仅定时模式落库，实时模式忽略
            schedule_time = (
                _norm_time(user_input.get(CONF_NOTIFY_SCHEDULE_TIME, ""))
                if mode == NOTIFY_MODE_SCHEDULED else ""
            )

            # 至少选择一个通知渠道
            if customize and not system and not entities:
                errors["base"] = "no_channel"

            if not errors:
                options = dict(self.config_entry.options)
                if self._is_notification_entry or customize:
                    options[CONF_NOTIFICATION] = {
                        CONF_NOTIFY_SYSTEM: system,
                        CONF_NOTIFY_ENTITIES: entities,
                        CONF_NOTIFY_STYLE: style,
                        CONF_NOTIFY_MODE: mode,
                        CONF_NOTIFY_SCHEDULE_TIME: schedule_time,
                    }
                else:
                    # 关闭自定义：删除条目级段，回退全局
                    options.pop(CONF_NOTIFICATION, None)
                return self._save(options)

        translations = await translation.async_get_translations(
            self.hass,
            self.hass.config.language,
            "selector",
            integrations=[DOMAIN],
        )
        style_prefix = (
            f"component.{DOMAIN}.selector.notify_style.options."
        )
        mode_prefix = (
            f"component.{DOMAIN}.selector.notify_mode.options."
        )
        style_options = [
            {
                "label": translations.get(f"{style_prefix}{style}", style),
                "value": style,
            }
            for style in NOTIFY_STYLES
        ]
        mode_options = [
            {
                "label": translations.get(f"{mode_prefix}{mode}", mode),
                "value": mode,
            }
            for mode in NOTIFY_MODES
        ]

        fields: dict[Any, Any] = {}
        if not self._is_notification_entry:
            fields[vol.Required(CONF_NOTIFY_CUSTOMIZE, default=has_override)] = (
                selector.BooleanSelector(selector.BooleanSelectorConfig())
            )
        fields.update(
            {
                vol.Required(
                    CONF_NOTIFY_SYSTEM,
                    default=bool(_default(CONF_NOTIFY_SYSTEM, False)),
                ): selector.BooleanSelector(selector.BooleanSelectorConfig()),
                vol.Optional(
                    CONF_NOTIFY_ENTITIES,
                    default=list(_default(CONF_NOTIFY_ENTITIES, []) or []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="notify", multiple=True
                    )
                ),
                vol.Optional(
                    CONF_NOTIFY_STYLE,
                    default=str(_default(CONF_NOTIFY_STYLE,
                                         NOTIFY_STYLE_HUMAN)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=style_options)
                ),
                vol.Optional(
                    CONF_NOTIFY_MODE,
                    default=str(_default(CONF_NOTIFY_MODE,
                                         NOTIFY_MODE_REALTIME)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=mode_options)
                ),
                # vol.Any(None, …) 为 HA 官方「可空选择器」写法：时间控件常驻但可留空
                vol.Optional(
                    CONF_NOTIFY_SCHEDULE_TIME,
                    default=(_default(CONF_NOTIFY_SCHEDULE_TIME, "") or None),
                ): vol.Any(
                    None,
                    selector.TimeSelector(selector.TimeSelectorConfig()),
                ),
            }
        )
        return self.async_show_form(
            step_id="notification",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    # ---- 库存条目：添加 / 修改 / 删除库存项 ----
    def _item_schema(self,
        item: dict[str, Any] | None = None,
        default_type: str | None = None,
        type_options: list[dict[str, str]] | None = None,
    ) -> vol.Schema:
        """自定义库存项表单（名称 / 关联类型 / 型号 / 单位 / 数量 / 库存阈值）。 """
        item = item or {}
        item_type = item.get(CONF_ITEM_TYPE) or default_type or ""
        return vol.Schema(
            {
                vol.Required(
                    CONF_ITEM_NAME, default=item.get(CONF_ITEM_NAME, "")
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Required(
                    CONF_ITEM_TYPE, default=item_type
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=type_options or [],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_MODEL, default=item.get(CONF_MODEL) or ""
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Required(
                    CONF_UNIT, default=item.get(CONF_UNIT) or "个"
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Required(
                    CONF_QUANTITY, default=item.get(CONF_QUANTITY, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-9999,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_STOCK_THRESHOLD,
                    default=item.get(CONF_STOCK_THRESHOLD, 1),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

    @staticmethod
    def _item_from_input( user_input: dict[str, Any], item_id: str ) -> dict[str, Any]:
        """把自定义表单输入规整成库存项。"""
        return {
            CONF_ITEM_ID: item_id,
            CONF_ITEM_NAME: user_input[CONF_ITEM_NAME],
            CONF_ITEM_TYPE: user_input.get(CONF_ITEM_TYPE),
            CONF_MODEL: user_input[CONF_MODEL],
            CONF_UNIT: user_input.get(CONF_UNIT) or None,
            CONF_QUANTITY: int(user_input[CONF_QUANTITY]),
            CONF_STOCK_THRESHOLD: int(user_input[CONF_STOCK_THRESHOLD]),
        }

    def _consumables_for_type(self,
        library: Library,
        cons_type: str | None,
    ) -> tuple[Consumable, ...]:
        """按前置选择的关联耗材类型过滤耗材；未选返回全部。"""
        if cons_type:
            return library.by_type(cons_type)
        results: list[Consumable] = []
        for item_type in library.types:
            results.extend(library.by_type(item_type))
        return tuple(results)

    async def async_step_add_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """添加库存二级界面：关联耗材类型（必选）+ 添加方式，一次选完。"""
        if user_input is not None:
            self._item_type = user_input.get(CONF_ITEM_TYPE)
            method = user_input.get(CONF_ADD_METHOD, ADD_METHOD_CONSUMABLE)
            if method == ADD_METHOD_CUSTOM_CONSUMABLE:
                return await self.async_step_custom_stock_item()
            return await self.async_step_consumable()

        labels = await self._type_labels()
        # 类型必选：默认预选首个库类型，避免空选
        first_type = next(iter(labels), "")
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ITEM_TYPE,
                    default=self._item_type or first_type,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._type_dropdown_options(labels),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_ADD_METHOD, default=ADD_METHOD_CONSUMABLE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[ADD_METHOD_CONSUMABLE, ADD_METHOD_CUSTOM_CONSUMABLE],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="add_method",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="add_item", data_schema=schema)

    async def async_step_consumable(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """常用耗材：耗材选择（按前置类型过滤）+ 数量 + 库存阈值。"""
        language = self.hass.config.language
        if user_input is not None:
            library = await self._library()
            consumable = library.get(user_input.get(CONF_CONSUMABLE_ID) or "")
            if consumable is None:
                return self.async_abort(reason="item_not_found")
            options = dict(self.config_entry.options)
            items = self._items
            items.append(
                {
                    CONF_ITEM_ID: uuid4().hex[:8],
                    CONF_ITEM_NAME: consumable.display_name(language),
                    CONF_ITEM_TYPE: consumable.cons_type,
                    CONF_CONSUMABLE_ID: consumable.id,
                    CONF_UNIT: consumable.unit,
                    CONF_QUANTITY: int(user_input[CONF_QUANTITY]),
                    CONF_STOCK_THRESHOLD: int(
                        user_input[CONF_STOCK_THRESHOLD]
                    ),
                }
            )
            options[CONF_STOCK_ITEMS] = items
            return self._save(options)

        library = await self._library()
        type_labels = await self._type_labels()
        results = self._consumables_for_type(library, self._item_type)
        schema = vol.Schema(
            {
                vol.Required(CONF_CONSUMABLE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._consumable_options(
                            results, type_labels, language
                        ),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_QUANTITY, default=0
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=-9999,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_STOCK_THRESHOLD, default=1
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="consumable", data_schema=schema)

    async def async_step_custom_stock_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """自定义耗材路线：完整手动表单；提交时写入用户库耗材段并回填 consumable_id。"""
        if user_input is not None:
            item_type = user_input.get(CONF_ITEM_TYPE) or ""
            name = str(user_input.get(CONF_ITEM_NAME, "")).strip()
            # 键缺失 → 表单默认「个」（与 _item_schema 的 default 对齐）；
            # 显式空字符串仍视为非法，由写库层必填校验拒绝
            unit = user_input.get(CONF_UNIT)
            if unit is None:
                unit = "个"
            model = user_input.get(CONF_MODEL) or ""
            # 写入用户库耗材段（返回生效 id；ID 冲突由写库层唯一性兜底）
            cid = await async_write_user_consumable(
                self.hass, item_type, model, name, unit
            )
            item = self._item_from_input(user_input, uuid4().hex[:8])
            item[CONF_CONSUMABLE_ID] = cid
            options = dict(self.config_entry.options)
            items = self._items
            items.append(item)
            options[CONF_STOCK_ITEMS] = items
            return self._save(options)

        labels = await self._type_labels()
        return self.async_show_form(
            step_id="custom_stock_item",
            data_schema=self._item_schema(
                default_type=self._item_type,
                type_options=self._type_dropdown_options(labels),
            ),
        )

    async def async_step_select_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """选择要修改的库存项。"""
        if user_input is not None:
            self._item_id = user_input[CONF_SELECTED_ITEM]
            return await self.async_step_edit_item()

        schema = vol.Schema(
            {
                vol.Required(CONF_SELECTED_ITEM): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._item_options(),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="select_item", data_schema=schema)

    async def async_step_edit_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """修改库存项：只允许改数量与库存阈值，其余字段保持原样。"""
        current = next(
            (
                item
                for item in self._items
                if item[CONF_ITEM_ID] == self._item_id
            ),
            None,
        )
        if current is None:
            return self.async_abort(reason="item_not_found")

        if user_input is not None:
            options = dict(self.config_entry.options)
            new_item = dict(current)
            new_item[CONF_QUANTITY] = int(user_input[CONF_QUANTITY])
            new_item[CONF_STOCK_THRESHOLD] = int(
                user_input[CONF_STOCK_THRESHOLD]
            )
            options[CONF_STOCK_ITEMS] = [
                new_item if item[CONF_ITEM_ID] == self._item_id else item
                for item in self._items
            ]
            return self._save(options)

        return self.async_show_form(
            step_id="edit_item",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_QUANTITY,
                        default=current.get(CONF_QUANTITY, 0),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=-9999,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_STOCK_THRESHOLD,
                        default=current.get(CONF_STOCK_THRESHOLD, 1),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
            description_placeholders={
                "name": current.get(CONF_ITEM_NAME, "")
            },
        )

    async def async_step_remove_items(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """删除库存项（多选）。"""
        if user_input is not None:
            removing = set(user_input.get(CONF_REMOVE_ITEMS, []))
            options = dict(self.config_entry.options)
            options[CONF_STOCK_ITEMS] = [
                item
                for item in self._items
                if item[CONF_ITEM_ID] not in removing
            ]
            return self._save(options)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REMOVE_ITEMS, default=[]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._item_options(),
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="remove_items", data_schema=schema
        )
