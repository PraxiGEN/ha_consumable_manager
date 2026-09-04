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
    CONF_THRESHOLD_UNIT, CONF_UNIT, CONSUMABLE_UNITS, CONSUMABLE_UNIT_PIECE, DEFAULT_THRESHOLD, CONF_LIFESPAN, CONF_LIFESPAN_UNIT, custom_consumable_entity_id,
    DEFAULT_THRESHOLD_TYPE, DEFAULT_THRESHOLD_UNIT, ENTRY_SORT_PREFIXES,
    ENTRY_TYPE_CUSTOM, ENTRY_TYPE_NOTIFICATION, ENTRY_TYPE_STOCK,
    CONF_TYPE_KEY, CONF_TYPE_NAME_ZH, CONF_TYPE_ICON, CONF_TYPE_THRESHOLD_TYPE,
    CONF_TYPE_THRESHOLD, CONF_TYPE_THRESHOLD_UNIT, OPERATORS,
    THRESHOLD_DEFAULT_OPERATOR, THRESHOLD_TYPES, THRESHOLD_UNIT_OPTIONS,
    UNIT_DAYS, THRESHOLD_TYPE_REMAINING_TIME, UNIT_HOURS, UNIT_MINUTES,
)
from .library import Consumable, ID_PATTERN, Library, LibraryError
from .user_library import (
    async_load_library,
    async_write_user_consumable,
    async_write_user_type,
)
from . import bindings

# 选择器模式常量（避免重复引用长路径）
_DROPDOWN = selector.SelectSelectorMode.DROPDOWN
_LIST = selector.SelectSelectorMode.LIST
_BOX = selector.NumberSelectorMode.BOX

async def _get_translations(hass: HomeAssistant) -> dict[str, str]:
    """取本集成 selector 类别翻译（按当前语言）。"""
    return await translation.async_get_translations(
        hass, hass.config.language, "selector", integrations=[DOMAIN]
    )

def _opt(value: str, label: str | None = None) -> dict[str, str]:
    """单选项 {label, value}；label 缺省等于 value。"""
    return {"label": label if label is not None else value, "value": value}

def _opt_pairs(seq) -> list[dict[str, str]]:
    """[{label: v, value: v}] 列表。"""
    return [{"label": v, "value": v} for v in seq]

def _sel(options, translation_key: str | None = None, *,
         multiple: bool = False, mode=_DROPDOWN):
    """下拉选择器（可多选）。"""
    cfg: dict[str, Any] = {"options": options, "mode": mode, "multiple": multiple}
    if translation_key is not None:
        cfg["translation_key"] = translation_key
    return selector.SelectSelector(selector.SelectSelectorConfig(**cfg))

def _num(min_val: float = 0, step: Any = 1):
    """数字框选择器。"""
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=min_val, step=step, mode=_BOX))

def _text():
    return selector.TextSelector(selector.TextSelectorConfig())

def _bool():
    return selector.BooleanSelector(selector.BooleanSelectorConfig())

def _entity(domain: str, multiple: bool = True):
    return selector.EntitySelector(selector.EntitySelectorConfig(
        domain=domain, multiple=multiple))

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
    translations: dict[str, str], language: str,
) -> list[dict[str, str]]:
    """添加界面「类型」下拉：通知 → 库存 → 各库类型 → 自定义类型。"""
    prefix = f"component.{DOMAIN}.selector.entry_type.options."
    def _opt_entry(value: str) -> dict[str, str]:
        return {"label": translations.get(f"{prefix}{value}", value),
                "value": value}
    options = [_opt_entry(ENTRY_TYPE_NOTIFICATION), _opt_entry(ENTRY_TYPE_STOCK)]
    options.extend(
        {"label": meta.display_name(language), "value": key}
        for key in library.types
        if (meta := library.type_meta(key)) is not None
    )
    options.append(_opt_entry(ENTRY_TYPE_CUSTOM))
    return options

def build_source_snapshots(hass: HomeAssistant,
    entity_ids: list[str],
) -> list[dict[str, Any]]:
    """反查实体/设备注册表，构建绑定实体快照（仅取展示用设备名）。"""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    snapshots: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        reg = ent_reg.async_get(entity_id)
        dev = dev_reg.async_get(reg.device_id) if reg and reg.device_id else None
        snapshots.append({
            "entity_id": entity_id,
            "device_name": getattr(dev, "name", None),
        })
    return snapshots

async def async_entry_type_title(hass: HomeAssistant,
    entry_type: str, library: Library | None = None,
) -> str:
    """按当前语言取条目标题。"""
    if library is not None and (meta := library.type_meta(entry_type)) is not None:
        title = meta.display_name(hass.config.language)
    else:
        title = (await _get_translations(hass)).get(
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
            await self.async_set_unique_id(entry_type)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=await async_entry_type_title(self.hass, entry_type, library),
                data={CONF_ENTRY_TYPE: entry_type},
            )
        options = _build_entry_type_options(
            library, await _get_translations(self.hass), self.hass.config.language)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ENTRY_TYPE): _sel(options),
            }),
        )

    async def async_step_custom_type(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """自定义类型向导：填类型键 + 名称 + 图标 + 默认阈值，写入用户库后建条目。"""
        errors: dict[str, str] = {}
        translations = await _get_translations(self.hass)
        library = await _load_library(self.hass)

        if user_input is not None:
            key = str(user_input.get(CONF_TYPE_KEY, "")).strip().lower()
            name = str(user_input.get(CONF_TYPE_NAME_ZH, "")).strip()
            icon = str(user_input.get(CONF_TYPE_ICON, "")).strip()
            threshold_type = user_input.get(CONF_TYPE_THRESHOLD_TYPE)
            threshold = user_input.get(CONF_TYPE_THRESHOLD)
            threshold_unit = user_input.get(CONF_TYPE_THRESHOLD_UNIT)

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
                threshold_val = float(threshold)
            except (TypeError, ValueError):
                errors["type_threshold"] = "invalid"

            if not errors:
                await async_write_user_type(self.hass, key, {
                    "name": name,
                    "icon": icon,
                    "default_threshold_type": threshold_type,
                    "default_threshold": threshold_val,
                    "default_threshold_unit": threshold_unit,
                })
                await self.async_set_unique_id(key)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name, data={CONF_ENTRY_TYPE: key})

        tprefix = f"component.{DOMAIN}.selector.threshold_type.options."
        uprefix = f"component.{DOMAIN}.selector.threshold_unit.options."
        return self.async_show_form(
            step_id="custom_type",
            data_schema=vol.Schema({
                vol.Required(CONF_TYPE_KEY): _text(),
                vol.Required(CONF_TYPE_NAME_ZH): _text(),
                vol.Optional(CONF_TYPE_ICON, default="mdi:package-variant"):
                    selector.IconSelector(selector.IconSelectorConfig()),
                vol.Required(CONF_TYPE_THRESHOLD_TYPE, default=DEFAULT_THRESHOLD_TYPE):
                    _sel([_opt(t, translations.get(f"{tprefix}{t}", t))
                          for t in THRESHOLD_TYPES]),
                vol.Required(CONF_TYPE_THRESHOLD, default=DEFAULT_THRESHOLD): _num(),
                vol.Required(CONF_TYPE_THRESHOLD_UNIT, default=DEFAULT_THRESHOLD_UNIT):
                    _sel([_opt(u, translations.get(f"{uprefix}{u}", u))
                          for u in THRESHOLD_UNIT_OPTIONS]),
            }),
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

    async def async_step_init(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """入口：通知条目直达表单；其余条目显示菜单（均含「通知」项）。"""
        if self._is_notification_entry:
            return await self.async_step_notification(user_input)
        if not self._is_stock_entry:
            # 耗材类型条目：1 级菜单直接列出分组管理各项
            menu = ["add_group"]
            if self._current_groups():
                menu += ["select_group", "remove_groups"]
            menu += ["threshold", "notification"]
            return self.async_show_menu(step_id="init", menu_options=menu)
        menu = ["add_item"]
        if self._items:
            menu += ["select_item", "remove_items"]
        menu.append("notification")
        return self.async_show_menu(step_id="init", menu_options=menu)

    @property
    def _is_stock_entry(self) -> bool:
        return self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_STOCK

    @property
    def _is_notification_entry(self) -> bool:
        """通知条目：仅承载全局通知配置，无菜单直接进表单。"""
        return self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION

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

    def _type_dropdown_options(self, labels: dict[str, str]) -> list[dict[str, str]]:
        """关联耗材类型下拉（仅库类型，类型必须关联）。"""
        return [{"label": label, "value": key} for key, label in labels.items()]

    def _consumable_options(self,
        results: tuple[Consumable, ...],
        type_labels: dict[str, str],
        language: str = "",
    ) -> list[dict[str, str]]:
        """耗材下拉（label = 类型名 › 耗材显示名）。"""
        return [
            {"label": f"{type_labels.get(c.cons_type, c.cons_type)} › "
                      f"{c.display_name(language)}", "value": c.id}
            for c in results
        ]

    def _consumables_for_type(self,
        library: Library, cons_type: str | None,
    ) -> tuple[Consumable, ...]:
        """按前置选择的关联耗材类型过滤耗材；未选返回全部。"""
        if cons_type:
            return library.by_type(cons_type)
        results: list[Consumable] = []
        for item_type in library.types:
            results.extend(library.by_type(item_type))
        return tuple(results)

    @property
    def _items(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_STOCK_ITEMS, []))

    def _item_options(self) -> list[dict[str, str]]:
        """库存项下拉（label = 自定义名称，value = 项 id）。"""
        return [
            {"label": item.get(CONF_ITEM_NAME, item[CONF_ITEM_ID]),
             "value": item[CONF_ITEM_ID]}
            for item in self._items
        ]

    def _save(self, options: dict[str, Any]) -> ConfigFlowResult:
        """写入条目配置并结束流程。"""
        return self.async_create_entry(title="", data=options)

    # ---- 库存条目：添加 / 修改 / 删除库存项 ----
    async def async_step_add_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """添加库存二级界面：关联耗材类型（必选）+ 添加方式，一次选完。"""
        if user_input is not None:
            self._item_type = user_input.get(CONF_ITEM_TYPE)
            method = user_input.get(CONF_ADD_METHOD, ADD_METHOD_CONSUMABLE)
            return await (self.async_step_custom_stock_item()
                          if method == ADD_METHOD_CUSTOM_CONSUMABLE
                          else self.async_step_consumable())
        labels = await self._type_labels()
        first_type = next(iter(labels), "")
        return self.async_show_form(
            step_id="add_item",
            data_schema=vol.Schema({
                vol.Required(CONF_ITEM_TYPE, default=self._item_type or first_type):
                    _sel(self._type_dropdown_options(labels)),
                vol.Required(CONF_ADD_METHOD, default=ADD_METHOD_CONSUMABLE):
                    _sel([ADD_METHOD_CONSUMABLE, ADD_METHOD_CUSTOM_CONSUMABLE],
                         "add_method"),
            }),
        )

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
            items = self._items
            items.append({
                CONF_ITEM_ID: uuid4().hex[:8],
                CONF_ITEM_NAME: consumable.display_name(language),
                CONF_ITEM_TYPE: consumable.cons_type,
                CONF_CONSUMABLE_ID: consumable.id,
                CONF_UNIT: consumable.unit,
                CONF_QUANTITY: int(user_input[CONF_QUANTITY]),
                CONF_STOCK_THRESHOLD: int(user_input[CONF_STOCK_THRESHOLD]),
            })
            options = dict(self.config_entry.options)
            options[CONF_STOCK_ITEMS] = items
            return self._save(options)

        library = await self._library()
        type_labels = await self._type_labels()
        results = self._consumables_for_type(library, self._item_type)
        return self.async_show_form(
            step_id="consumable",
            data_schema=vol.Schema({
                vol.Required(CONF_CONSUMABLE_ID):
                    _sel(self._consumable_options(results, type_labels, language)),
                vol.Required(CONF_QUANTITY, default=0): _num(-9999),
                vol.Required(CONF_STOCK_THRESHOLD, default=1): _num(),
            }),
        )

    async def async_step_custom_stock_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """自定义耗材路线：完整手动表单；提交写入用户库耗材段并回填。"""
        if user_input is not None:
            item_type = user_input.get(CONF_ITEM_TYPE) or ""
            name = str(user_input.get(CONF_ITEM_NAME, "")).strip()
            raw_unit = user_input.get(CONF_UNIT)
            # 显式清空单位 → 报错（宁报错不写坏库）；未提交才兜底默认单位
            unit = raw_unit or CONSUMABLE_UNIT_PIECE
            model = user_input.get(CONF_MODEL) or ""
            errors: dict[str, str] = {}
            if not name:
                errors[CONF_ITEM_NAME] = "required"
            if not model:
                errors[CONF_MODEL] = "required"
            elif not model.isascii():
                # model 参与 id 推导/去重且用户库可对外 PR，写入层强制 ASCII，
                # 此处预检给出明确文案（避免落入 LibraryError 的笼统映射）
                errors[CONF_MODEL] = "model_ascii"
            if raw_unit is not None and not raw_unit:
                errors[CONF_UNIT] = "required"
            if not errors:
                try:
                    cid = await async_write_user_consumable(
                        self.hass, item_type, model, name, unit)
                except LibraryError:
                    errors[CONF_MODEL] = "required"
            if errors:
                return self.async_show_form(
                    step_id="custom_stock_item",
                    data_schema=self._item_schema(
                        item={
                            CONF_ITEM_NAME: name,
                            CONF_ITEM_TYPE: item_type,
                            CONF_MODEL: model,
                            CONF_UNIT: unit,
                        },
                        default_type=self._item_type,
                        type_options=self._type_dropdown_options(
                            await self._type_labels()),
                    ),
                    errors=errors,
                )
            items = self._items
            items.append({
                CONF_ITEM_ID: uuid4().hex[:8],
                CONF_ITEM_NAME: name,
                CONF_ITEM_TYPE: item_type,
                CONF_MODEL: model,
                CONF_UNIT: unit,
                CONF_QUANTITY: int(user_input.get(CONF_QUANTITY, 0)),
                CONF_STOCK_THRESHOLD: int(user_input.get(CONF_STOCK_THRESHOLD, 1)),
                CONF_CONSUMABLE_ID: cid,
            })
            options = dict(self.config_entry.options)
            options[CONF_STOCK_ITEMS] = items
            return self._save(options)

        return self.async_show_form(
            step_id="custom_stock_item",
            data_schema=self._item_schema(
                default_type=self._item_type,
                type_options=self._type_dropdown_options(await self._type_labels()),
            ),
        )

    async def async_step_select_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """选择要修改的库存项。"""
        if user_input is not None:
            self._item_id = user_input[CONF_SELECTED_ITEM]
            return await self.async_step_edit_item()
        return self.async_show_form(
            step_id="select_item",
            data_schema=vol.Schema({
                vol.Required(CONF_SELECTED_ITEM): _sel(self._item_options()),
            }),
        )

    async def async_step_edit_item(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """修改库存项：只允许改数量与库存阈值，其余字段保持原样。"""
        current = next((it for it in self._items
                        if it[CONF_ITEM_ID] == self._item_id), None)
        if current is None:
            return self.async_abort(reason="item_not_found")
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_STOCK_ITEMS] = [
                {**it, CONF_QUANTITY: int(user_input[CONF_QUANTITY]),
                 CONF_STOCK_THRESHOLD: int(user_input[CONF_STOCK_THRESHOLD])}
                if it[CONF_ITEM_ID] == self._item_id else it
                for it in self._items
            ]
            return self._save(options)
        return self.async_show_form(
            step_id="edit_item",
            data_schema=vol.Schema({
                vol.Required(CONF_QUANTITY, default=current.get(CONF_QUANTITY, 0)):
                    _num(-9999),
                vol.Required(CONF_STOCK_THRESHOLD,
                             default=current.get(CONF_STOCK_THRESHOLD, 1)): _num(),
            }),
            description_placeholders={"name": current.get(CONF_ITEM_NAME, "")},
        )

    async def async_step_remove_items(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """删除库存项（多选）。"""
        if user_input is not None:
            removing = set(user_input.get(CONF_REMOVE_ITEMS, []))
            options = dict(self.config_entry.options)
            options[CONF_STOCK_ITEMS] = [
                it for it in self._items if it[CONF_ITEM_ID] not in removing]
            return self._save(options)
        return self.async_show_form(
            step_id="remove_items",
            data_schema=vol.Schema({
                vol.Optional(CONF_REMOVE_ITEMS, default=[]):
                    _sel(self._item_options(), multiple=True, mode=_LIST),
            }),
        )

    def _unit_options(self) -> list[str]:
        """耗材单位下拉（locale 无关键 + translation_key 自动翻译）。"""
        return list(CONSUMABLE_UNITS)

    def _item_schema(self,
        item: dict[str, Any] | None = None,
        default_type: str | None = None,
        type_options: list[dict[str, str]] | None = None,
    ) -> vol.Schema:
        """自定义库存项表单（名称 / 关联类型 / 型号 / 单位 / 数量 / 阈值）。"""
        item = item or {}
        item_type = item.get(CONF_ITEM_TYPE) or default_type or ""
        return vol.Schema({
            vol.Required(CONF_ITEM_NAME, default=item.get(CONF_ITEM_NAME, "")): _text(),
            vol.Required(CONF_ITEM_TYPE, default=item_type): _sel(type_options or []),
            vol.Required(CONF_MODEL, default=item.get(CONF_MODEL) or ""): _text(),
            vol.Optional(CONF_UNIT, default=item.get(CONF_UNIT) or CONSUMABLE_UNIT_PIECE):
                _sel(self._unit_options(), "units"),
            vol.Required(CONF_QUANTITY, default=item.get(CONF_QUANTITY, 0)):
                _num(-9999),
            vol.Required(CONF_STOCK_THRESHOLD,
                         default=item.get(CONF_STOCK_THRESHOLD, 1)): _num(),
        })

    # ---- 通用类型条目：绑定分组（多分组 → 多诊断实体） ----
    async def async_step_add_group(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """新建分组（菜单项）：先选分组类别，再进入对应表单。"""
        if user_input is not None:
            self._group_edit_idx = None
            kind = user_input.get(CONF_GROUP_KIND, GROUP_KIND_BINDING)
            return await (self.async_step_custom_entity()
                          if kind == GROUP_KIND_CUSTOM
                          else self.async_step_group())
        return self.async_show_form(
            step_id="add_group",
            data_schema=vol.Schema({
                vol.Required(CONF_GROUP_KIND, default=GROUP_KIND_BINDING):
                    # 标签经 selector.group_kind.options.* 翻译（不在代码硬编码文案）
                    _sel([GROUP_KIND_BINDING, GROUP_KIND_CUSTOM], "group_kind"),
            }),
        )

    async def async_step_select_group(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """修改分组：先选一个分组，再进入对应编辑表单（仿库存 select_item）。"""
        if user_input is not None:
            gid = user_input[CONF_SELECTED_GROUP]
            groups = self._current_groups()
            # 显式查找命中索引：不依赖循环变量泄漏；group_id 未命中
            # （表单陈旧 / options 被并发修改）时中止而不是走错表单分支
            idx = next(
                (i for i, g in enumerate(groups)
                 if g.get(CONF_GROUP_ID) == gid),
                None,
            )
            if idx is None:
                return self.async_abort(reason="group_not_found")
            self._group_edit_idx = idx
            group = groups[idx]
            return await (self.async_step_custom_entity()
                          if group.get(CONF_GROUP_KIND) == GROUP_KIND_CUSTOM
                          else self.async_step_group())
        return self.async_show_form(
            step_id="select_group",
            data_schema=vol.Schema({
                vol.Required(CONF_SELECTED_GROUP): _sel(self._group_options()),
            }),
        )

    async def async_step_remove_groups(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """删除分组（多选，仿库存 remove_items）。"""
        if user_input is not None:
            removing = set(user_input.get(CONF_REMOVE_GROUPS, []))
            return self._save_groups([
                g for g in self._current_groups()
                if g.get(CONF_GROUP_ID) not in removing])
        return self.async_show_form(
            step_id="remove_groups",
            data_schema=vol.Schema({
                vol.Optional(CONF_REMOVE_GROUPS, default=[]):
                    _sel(self._group_options(), multiple=True, mode=_LIST),
            }),
        )

    async def async_step_group(self,
        user_input: dict[str, Any] | None = None, idx: int | None = None,
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
                        self.hass, sorted(set(manual))),
                    # 正则仅存规则本身，运行时由协调器动态匹配（不再提交时合并）
                    CONF_ENTITY_REGEX: regex,
                }
                if user_input.get("override_threshold"):
                    for k in (CONF_THRESHOLD_TYPE, CONF_THRESHOLD,
                              CONF_THRESHOLD_UNIT, CONF_THRESHOLD_OPERATOR):
                        new_group[k] = user_input[k]
                if editing:
                    groups[self._group_edit_idx] = new_group
                else:
                    groups.append(new_group)
                return self._save_groups(groups)

        default_name = group.get(CONF_GROUP_NAME, "")
        default_entities = [s["entity_id"] for s in group.get(CONF_SOURCE_ENTITIES, [])
                            if s.get("entity_id")]
        override_on = any(k in group for k in
                          (CONF_THRESHOLD, CONF_THRESHOLD_TYPE, CONF_THRESHOLD_UNIT,
                           CONF_THRESHOLD_OPERATOR))
        d_type, d_val, d_unit = await self._threshold_defaults()
        t_type = group.get(CONF_THRESHOLD_TYPE, d_type)
        t_val = group.get(CONF_THRESHOLD, d_val)
        t_unit = group.get(CONF_THRESHOLD_UNIT, d_unit)
        t_op = group.get(CONF_THRESHOLD_OPERATOR, THRESHOLD_DEFAULT_OPERATOR[t_type])
        return self.async_show_form(
            step_id="group",
            data_schema=vol.Schema({
                vol.Required(CONF_GROUP_NAME, default=default_name): _text(),
                vol.Optional(CONF_ENTITY_REGEX, default=group.get(CONF_ENTITY_REGEX, "")):
                    _text(),
                vol.Optional(CONF_SOURCE_ENTITIES, default=default_entities):
                    _entity("sensor"),
                vol.Optional("override_threshold", default=override_on): _bool(),
                vol.Optional(CONF_THRESHOLD_TYPE, default=t_type):
                    _sel(_opt_pairs(THRESHOLD_TYPES), "threshold_type"),
                vol.Optional(CONF_THRESHOLD, default=t_val): _num(step="any"),
                vol.Optional(CONF_THRESHOLD_UNIT, default=t_unit):
                    _sel(_opt_pairs(THRESHOLD_UNIT_OPTIONS), "threshold_unit"),
                vol.Optional(CONF_THRESHOLD_OPERATOR, default=t_op):
                    _sel(_opt_pairs(OPERATORS), "threshold_operator"),
            }),
            errors=errors,
        )

    async def async_step_custom_entity(self,
        user_input: dict[str, Any] | None = None, idx: int | None = None,
    ) -> ConfigFlowResult:
        """自定义耗材实体：上=生成倒计时数据实体的数据，下=条目级阈值（同分组）。"""
        errors: dict[str, str] = {}
        if idx is not None:
            self._group_edit_idx = idx
        groups = self._current_groups()
        edit_idx = getattr(self, "_group_edit_idx", None)
        editing = edit_idx is not None and 0 <= edit_idx < len(groups)
        group = groups[edit_idx] if editing else {}

        # 绑定耗材下拉：只列出本条目关联类型（cons_type）的耗材，
        # 让自定义耗材实体（无实体 ID）直接关联到集成内的具体耗材。
        language = self.hass.config.language
        library = await self._library()
        type_labels = await self._type_labels()
        cons_type = self._item_type or self.config_entry.data.get(CONF_ENTRY_TYPE)
        consumable_options = self._consumable_options(
            self._consumables_for_type(library, cons_type), type_labels, language)

        # 编辑时回填已绑定耗材（绑定存于 bindings Store 层，不在 group options）
        default_consumable = ""
        if editing:
            default_consumable = bindings.get_binding(
                self.hass,
                custom_consumable_entity_id(
                    self.config_entry.entry_id, group.get(CONF_GROUP_ID, "")),
            ) or ""
        # 回填值必须仍是合法选项才可作为 default：未绑定（空串）或耗材已删除
        # （悬空引用）若塞进 default，用户直接提交会被 selector 拒绝并报
        # "value must be one of [...]"。不合法时不给默认值，保存即清除。
        valid_default = bool(default_consumable) and any(
            opt["value"] == default_consumable for opt in consumable_options)
        consumable_field = (
            vol.Optional(CONF_CONSUMABLE_ID, default=default_consumable)
            if valid_default
            else vol.Optional(CONF_CONSUMABLE_ID)
        )

        if user_input is not None:
            name = str(user_input.get(CONF_GROUP_NAME) or "").strip()
            added_at = user_input.get(CONF_ADDED_AT)
            lifespan = user_input.get(CONF_LIFESPAN)
            if not name:
                errors["name"] = "required"
            elif not added_at:
                errors["added_at"] = "required"
            elif lifespan in (None, ""):
                errors["lifespan"] = "required"
            elif (
                not isinstance(lifespan, (int, float))
                or isinstance(lifespan, bool)
                or lifespan <= 0
            ):
                # 冗余校验（UI 数字框之外的第二道闸）：非正数寿命会让
                # 倒计时恒为逾期 → 永久误报
                errors["lifespan"] = "invalid"
            else:
                gid = group.get(CONF_GROUP_ID) or uuid4().hex[:8]
                synthetic_id = custom_consumable_entity_id(
                    self.config_entry.entry_id, gid)
                new_group: dict[str, Any] = {
                    CONF_GROUP_ID: gid,
                    CONF_GROUP_NAME: name,
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: added_at,
                    CONF_LIFESPAN: lifespan,
                    CONF_LIFESPAN_UNIT: user_input[CONF_LIFESPAN_UNIT],
                }
                # 下部分：覆盖条目级阈值（与「添加分组」同构），未勾选则不写
                if user_input.get("override_threshold"):
                    for k in (CONF_THRESHOLD_TYPE, CONF_THRESHOLD,
                              CONF_THRESHOLD_UNIT, CONF_THRESHOLD_OPERATOR):
                        new_group[k] = user_input[k]
                if editing:
                    groups[self._group_edit_idx] = new_group
                else:
                    groups.append(new_group)
                result = self._save_groups(groups)
                # 绑定耗材：写 bindings Store 层（key = 合成数据实体 id），
                # 实体生成后 bind_entity 服务也能按此 id 重绑 / 解绑。
                await self._sync_custom_binding(
                    synthetic_id, user_input.get(CONF_CONSUMABLE_ID) or None)
                return result

        default_name = group.get(CONF_GROUP_NAME, self.config_entry.title)
        default_added_at = group.get(
            CONF_ADDED_AT, datetime.date.today().isoformat())
        default_lifespan = group.get(CONF_LIFESPAN, 180)
        default_lifespan_unit = group.get(CONF_LIFESPAN_UNIT, UNIT_DAYS)

        # 阈值默认值：新建自定义组默认开启覆盖、剩余时间 < 0（到期提醒）
        override_on = bool(
            group.get(CONF_THRESHOLD) is not None
            or group.get(CONF_THRESHOLD_TYPE) is not None
            or group.get(CONF_THRESHOLD_UNIT) is not None
            or group.get(CONF_THRESHOLD_OPERATOR) is not None
        )
        if not editing:
            override_on = True
            d_type = THRESHOLD_TYPE_REMAINING_TIME
            d_val = 0
            d_unit = default_lifespan_unit
        else:
            d_type, d_val, d_unit = await self._threshold_defaults()
        t_type = group.get(CONF_THRESHOLD_TYPE, d_type)
        t_val = group.get(CONF_THRESHOLD, d_val)
        t_unit = group.get(CONF_THRESHOLD_UNIT, d_unit)
        t_op = group.get(
            CONF_THRESHOLD_OPERATOR, THRESHOLD_DEFAULT_OPERATOR[t_type])
        return self.async_show_form(
            step_id="custom_entity",
            data_schema=vol.Schema({
                vol.Required(CONF_GROUP_NAME, default=default_name): _text(),
                consumable_field:
                    _sel(consumable_options),
                vol.Required(CONF_ADDED_AT, default=default_added_at):
                    selector.DateSelector(),
                vol.Required(CONF_LIFESPAN, default=default_lifespan):
                    _num(step="any"),
                vol.Required(CONF_LIFESPAN_UNIT, default=default_lifespan_unit):
                    _sel(_opt_pairs((UNIT_DAYS, UNIT_HOURS, UNIT_MINUTES)),
                         "threshold_unit"),
                vol.Optional("override_threshold", default=override_on): _bool(),
                vol.Optional(CONF_THRESHOLD_TYPE, default=t_type):
                    _sel(_opt_pairs(THRESHOLD_TYPES), "threshold_type"),
                vol.Optional(CONF_THRESHOLD, default=t_val): _num(step="any"),
                vol.Optional(CONF_THRESHOLD_UNIT, default=t_unit):
                    _sel(_opt_pairs(THRESHOLD_UNIT_OPTIONS), "threshold_unit"),
                vol.Optional(CONF_THRESHOLD_OPERATOR, default=t_op):
                    _sel(_opt_pairs(OPERATORS), "threshold_operator"),
            }),
            errors=errors,
        )

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

    async def _sync_custom_binding(self,
            entity_id: str, consumable_id: str | None) -> None:
        """自定义耗材实体创建/编辑后，把「绑定耗材」写入 bindings Store 层。"""
        if consumable_id:
            await bindings.async_set_binding(
                self.hass, entity_id, consumable_id)
        else:
            await bindings.async_remove_binding(self.hass, entity_id)

    def _group_options(self) -> list[dict[str, str]]:
        """分组下拉（label = 分组名；无名兜底分组 id，locale 无关）。"""
        return [
            {"label": g.get(CONF_GROUP_NAME) or g.get(CONF_GROUP_ID, "group"),
             "value": g.get(CONF_GROUP_ID, "")}
            for g in self._current_groups()
        ]

    async def _threshold_defaults(self):
        """阈值默认兜底链：库类型元数据 → 通用兜底。"""
        meta = (await self._library()).type_meta(
            self.config_entry.data.get(CONF_ENTRY_TYPE, ""))
        if meta:
            return (meta.default_threshold_type, meta.default_threshold,
                    meta.default_threshold_unit)
        return DEFAULT_THRESHOLD_TYPE, DEFAULT_THRESHOLD, DEFAULT_THRESHOLD_UNIT

    async def async_step_threshold(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """设置阈值（类型 + 阈值 + 单位 + 计算方式）并保存。"""
        if user_input is not None:
            options = dict(self.config_entry.options)
            for k in (CONF_THRESHOLD_TYPE, CONF_THRESHOLD, CONF_THRESHOLD_UNIT,
                      CONF_THRESHOLD_OPERATOR):
                options[k] = user_input[k]
            return self._save(options)

        # 阈值默认值兜底链：已存配置 → 库类型元数据 → 通用兜底
        d_type, d_val, d_unit = await self._threshold_defaults()
        threshold_type = self.config_entry.options.get(CONF_THRESHOLD_TYPE, d_type)
        threshold = self.config_entry.options.get(CONF_THRESHOLD, d_val)
        unit = self.config_entry.options.get(CONF_THRESHOLD_UNIT, d_unit)
        operator = self.config_entry.options.get(
            CONF_THRESHOLD_OPERATOR, THRESHOLD_DEFAULT_OPERATOR[threshold_type])
        return self.async_show_form(
            step_id="threshold",
            data_schema=self._threshold_schema(
                threshold_type, threshold, unit, operator),
        )

    def _threshold_schema(self,
        threshold_type: str, threshold: float, unit: str, operator: str,
    ) -> vol.Schema:
        """阈值配置表单（类型 + 阈值 + 单位 + 计算方式）。"""
        return vol.Schema({
            vol.Required(CONF_THRESHOLD_TYPE, default=threshold_type):
                _sel(list(THRESHOLD_TYPES), "threshold_type"),
            vol.Required(CONF_THRESHOLD, default=threshold): _num(step="any"),
            vol.Required(CONF_THRESHOLD_UNIT, default=unit):
                _sel(list(THRESHOLD_UNIT_OPTIONS), "threshold_unit"),
            vol.Required(CONF_THRESHOLD_OPERATOR, default=operator):
                _sel(list(OPERATORS), "threshold_operator"),
        })

    # ---- 通知配置（全局通知条目 + 条目级覆盖共用表单） ----
    async def async_step_notification(self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """通知配置表单：渠道多选 + 推送模式 + 消息样式。 """
        errors: dict[str, str] = {}
        current_section = self.config_entry.options.get(CONF_NOTIFICATION)
        has_override = isinstance(current_section, dict)
        # 预填默认：条目级段优先，无段时用全局段（展示当前生效值）
        defaults = (current_section if has_override
                    else self._global_notification_section())

        def _default(key: str, fallback: Any) -> Any:
            value = defaults.get(key)
            return value if value is not None else fallback

        if user_input is not None:
            customize = (bool(user_input.get(CONF_NOTIFY_CUSTOMIZE))
                         if not self._is_notification_entry else True)
            system = bool(user_input.get(CONF_NOTIFY_SYSTEM))
            entities = list(user_input.get(CONF_NOTIFY_ENTITIES, []) or [])
            style = str(user_input.get(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN))
            mode = str(user_input.get(CONF_NOTIFY_MODE, NOTIFY_MODE_REALTIME))
            # 时间字段可留空（vol.Any(None, …)）；仅定时模式落库，实时模式忽略
            schedule_time = (_norm_time(user_input.get(CONF_NOTIFY_SCHEDULE_TIME, ""))
                             if mode == NOTIFY_MODE_SCHEDULED else "")
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

        translations = await _get_translations(self.hass)
        style_prefix = f"component.{DOMAIN}.selector.notify_style.options."
        mode_prefix = f"component.{DOMAIN}.selector.notify_mode.options."
        style_options = [{"label": translations.get(f"{style_prefix}{s}", s),
                          "value": s} for s in NOTIFY_STYLES]
        mode_options = [{"label": translations.get(f"{mode_prefix}{m}", m),
                         "value": m} for m in NOTIFY_MODES]

        fields: dict[Any, Any] = {}
        if not self._is_notification_entry:
            fields[vol.Required(CONF_NOTIFY_CUSTOMIZE, default=has_override)] = _bool()
        fields.update({
            vol.Required(CONF_NOTIFY_SYSTEM,
                         default=bool(_default(CONF_NOTIFY_SYSTEM, False))): _bool(),
            vol.Optional(CONF_NOTIFY_ENTITIES,
                         default=list(_default(CONF_NOTIFY_ENTITIES, []) or [])):
                _entity("notify"),
            vol.Optional(CONF_NOTIFY_STYLE,
                         default=str(_default(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN))):
                _sel(style_options),
            vol.Optional(CONF_NOTIFY_MODE,
                         default=str(_default(CONF_NOTIFY_MODE, NOTIFY_MODE_REALTIME))):
                _sel(mode_options),
            # vol.Any(None, …) 为 HA 官方「可空选择器」写法：时间控件常驻但可留空
            vol.Optional(CONF_NOTIFY_SCHEDULE_TIME,
                         default=(_default(CONF_NOTIFY_SCHEDULE_TIME, "") or None)):
                vol.Any(None, selector.TimeSelector(selector.TimeSelectorConfig())),
        })
        return self.async_show_form(
            step_id="notification", data_schema=vol.Schema(fields), errors=errors)

    def _global_notification_section(self) -> dict[str, Any]:
        """全局通知条目的通知段（无则空字典，作表单预填默认值）。"""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION:
                section = entry.options.get(CONF_NOTIFICATION)
                if isinstance(section, dict):
                    return section
        return {}