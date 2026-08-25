"""耗材管理器 服务平台。"""
from __future__ import annotations

import json
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import SupportsResponse

from .const import (
    DOMAIN, CONF_CONSUMABLE_ID, CONF_ITEM_ID, CONF_ITEM_NAME,
    CONF_ITEM_TYPE, CONF_SOURCE_ENTITIES, CONF_STOCK_ITEMS,
    ENTRY_TYPE_STOCK, THRESHOLD_TYPES, THRESHOLD_UNIT_OPTIONS,
)
from .coordinator import (
    ConsumableManagerData,
    ConsumableTypeCoordinator,
    StockCoordinator,
    _find_stock_coordinator,
)
from .config_flow import build_source_snapshots
from .library import ID_PATTERN, Consumable, Library, LibraryError
from .user_library import (
    async_load_library,
    async_write_user_consumable,
    async_write_user_device,
    async_write_user_device_consumable,
    async_write_user_type,
    user_library_path,
)

# ---- 定位辅助 ----
def _type_coordinators( hass: HomeAssistant, ) -> list[ConsumableTypeCoordinator]:
    """全部耗材类型条目的协调器（经 entry.runtime_data 定位）。"""
    result: list[ConsumableTypeCoordinator] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        # getattr 防御：加载过程中部分条目尚未写入 runtime_data
        data = getattr(entry, "runtime_data", None)
        if not isinstance(data, ConsumableManagerData):
            continue
        coord = data.coordinator
        if isinstance(coord, ConsumableTypeCoordinator):
            result.append(coord)
    return result

def _find_type_coordinator(hass: HomeAssistant,
    cons_type: str,
) -> ConsumableTypeCoordinator | None:
    for coord in _type_coordinators(hass):
        if coord.cons_type == cons_type:
            return coord
    return None

async def _refresh_coordinator_libraries(hass: HomeAssistant) -> None:
    """写库服务成功后：重新加载合并库并更新全部类型协调器的库引用 + 触发刷新，
    使待办/通知描述立即反映新设备映射 / 新耗材（无需重启 HA）。"""
    library = await async_load_library(hass)
    for coord in _type_coordinators(hass):
        coord.update_library(library)
        await coord.async_request_refresh()

def _coerce_item_id( hass: HomeAssistant, raw: str | None ) -> str | None:
    """把库存项选择框的值规整为 item_id（实体选择器 → 属性/注册表反查）。"""
    if not raw:
        return None
    if "." not in raw:
        return raw
    state = hass.states.get(raw)
    if state is not None:
        item_id = state.attributes.get(CONF_ITEM_ID)
        if item_id:
            return item_id
    reg = er.async_get(hass)
    reg_entry = reg.async_get(raw)
    if reg_entry is not None and reg_entry.unique_id:
        return str(reg_entry.unique_id).split("_", 1)[1]
    return raw

# ---- 服务：实体耗材绑定 ----
async def _resolve_consumable(
    hass: HomeAssistant,
    library: Library,
    entity_id: str,
    consumable_id: str | None,
) -> tuple[Consumable, str]:
    """解析目标耗材：手动指定优先，否则按实体设备自动匹配。"""
    if consumable_id:
        consumable = library.get(consumable_id)
        if consumable is None:
            raise ServiceValidationError(
                f"耗材库中不存在耗材 {consumable_id}"
            )
        return consumable, "manual"

    ent_reg = er.async_get(hass)
    reg_entry = ent_reg.async_get(entity_id)
    device = None
    if reg_entry is not None and getattr(reg_entry, "device_id", None):
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(reg_entry.device_id)
    manufacturer = getattr(device, "manufacturer", None) if device else None
    model = getattr(device, "model", None) if device else None
    matches = library.find_compatible(manufacturer, model)
    if not matches:
        raise ServiceValidationError(
            "未找到与该设备匹配的耗材，请手动指定 consumable_id"
        )
    if len(matches) > 1:
        language = hass.config.language
        names = "、".join(c.display_name(language) for c in matches)
        raise ServiceValidationError(
            f"匹配到多个耗材（{names}），请手动指定 consumable_id"
        )
    return matches[0], "auto"

def _link_stock_item(hass: HomeAssistant,
    item_id: str,
    consumable: Consumable,
) -> None:
    """把库存项关联到耗材（写入 consumable_id 与 item_type）。"""
    stock = _find_stock_coordinator(hass)
    if stock is None:
        raise ServiceValidationError("未配置库存条目，无法关联库存项")
    updated = False
    items: list[dict[str, Any]] = []
    for item in stock.items:
        new_item = dict(item)
        if new_item.get(CONF_ITEM_ID) == item_id:
            new_item[CONF_CONSUMABLE_ID] = consumable.id
            new_item[CONF_ITEM_TYPE] = consumable.cons_type
            updated = True
        items.append(new_item)
    if not updated:
        raise ServiceValidationError(f"未找到库存项 {item_id}")
    options = dict(stock.options)
    options[CONF_STOCK_ITEMS] = items
    hass.config_entries.async_update_entry(stock.entry, options=options)

async def async_bind_entity( hass: HomeAssistant, call: ServiceCall ) -> dict[str, Any]:
    """绑定实体到耗材（自动匹配或手动指定），可选关联库存项，可选沉淀设备映射入库。"""
    entity_id = call.data.get("entity_id")
    consumable_id = call.data.get("consumable_id")
    item_id = _coerce_item_id(hass, call.data.get("item"))
    record_device = bool(call.data.get("record_device"))
    if not entity_id:
        raise ServiceValidationError("缺少 entity_id")
    # 未手输耗材但选择了关联库存项 → 继承库存项已关联的 consumable_id
    inherited = False
    if not consumable_id and item_id:
        stock = _find_stock_coordinator(hass)
        if stock is not None:
            item = next(
                (i for i in stock.items
                 if i.get(CONF_ITEM_ID) == item_id),
                None,
            )
            if item is not None and item.get(CONF_CONSUMABLE_ID):
                consumable_id = str(item[CONF_CONSUMABLE_ID])
                inherited = True
    library = await async_load_library(hass)
    consumable, matched_by = await _resolve_consumable(
        hass, library, entity_id, consumable_id
    )
    if inherited:
        matched_by = "stock"
    coord = _find_type_coordinator(hass, consumable.cons_type)
    if coord is None:
        raise ServiceValidationError(
            f"尚未添加「{consumable.cons_type}」类型的集成条目，"
            "请先在集成中添加该类型"
        )
    # 把解析到的具体耗材 id 写入快照，供「查询绑定」回显耗材型号
    options = dict(coord.options)
    existing = coord.source_snapshots
    new_snap = build_source_snapshots(hass, [entity_id])[0]
    new_snap["consumable_id"] = consumable.id
    if entity_id in coord.source_entities:
        # 已绑定：更新既有快照的耗材型号（重绑场景）
        merged: list[dict[str, Any]] = []
        for s in existing:
            if s.get("entity_id") == entity_id:
                merged.append({**s, "consumable_id": consumable.id})
            else:
                merged.append(s)
        options[CONF_SOURCE_ENTITIES] = merged
    else:
        options[CONF_SOURCE_ENTITIES] = existing + [new_snap]
    hass.config_entries.async_update_entry(coord.entry, options=options)
    await coord.async_request_refresh()

    if item_id:
        _link_stock_item(hass, item_id, consumable)

    # 可选：把「厂商+型号→耗材」合并追加进用户库设备映射（不覆盖既有）
    device_recorded = False
    if record_device:
        device = None
        ent_reg = er.async_get(hass)
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry and getattr(reg_entry, "device_id", None):
            dev_reg = dr.async_get(hass)
            device = dev_reg.async_get(reg_entry.device_id)
        manufacturer = getattr(device, "manufacturer", None) if device else None
        model = getattr(device, "model", None) if device else None
        if manufacturer and model:
            try:
                await async_write_user_device_consumable(
                    hass, manufacturer, model, consumable.id,
                    getattr(device, "name", None),
                )
                device_recorded = True
            except LibraryError:
                device_recorded = False

    return {
        "entity_id": entity_id,
        "consumable_id": consumable.id,
        "consumable_name": consumable.display_name(hass.config.language),
        "entry_type": coord.cons_type,
        "matched_by": matched_by,
        "item_id": item_id,
        "record_device": record_device,
        "device_recorded": device_recorded,
    }

def _resolve_entity_info(
    hass: HomeAssistant,
    entity_id: str,
) -> dict[str, Any]:
    """从实体/设备/区域注册表解析实体所属设备信息，供「绑定前查询」使用。

    实体未注册、未挂设备、或设备无厂商/型号时，对应字段为 None，
    不报错（这类实体仍可纯绑定，只是自动匹配用不上）。
    """
    info: dict[str, Any] = {
        "entity_id": entity_id,
        "device_id": None,
        "manufacturer": None,
        "model": None,
        "device_name": None,
        "area_id": None,
        "area_name": None,
    }
    ent_reg = er.async_get(hass)
    reg_entry = ent_reg.async_get(entity_id)
    if reg_entry is None:
        return info
    device_id = getattr(reg_entry, "device_id", None)
    if not device_id:
        return info
    info["device_id"] = device_id
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if device is None:
        return info
    info["manufacturer"] = getattr(device, "manufacturer", None)
    info["model"] = getattr(device, "model", None)
    info["device_name"] = getattr(device, "name", None)
    area_id = getattr(device, "area_id", None)
    if area_id:
        info["area_id"] = area_id
        area_reg = ar.async_get(hass)
        area = area_reg.async_get_area(area_id)
        if area is not None:
            info["area_name"] = getattr(area, "name", None)
    return info

async def async_query_binding(hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """查询绑定关系：按实体 / 耗材 / 库存项过滤。

    传入 entity_id 时，额外返回该实体的设备信息（entity_info）与按厂商+
    型号从库里推荐的耗材（suggested），便于「先查设备、再绑定」。
    """
    entity_id = call.data.get("entity_id")
    consumable_id = call.data.get("consumable_id")
    item_id = _coerce_item_id(hass, call.data.get("item"))
    library = await async_load_library(hass)

    filter_type: str | None = None
    if consumable_id:
        consumable = library.get(consumable_id)
        filter_type = consumable.cons_type if consumable else None
    if item_id:
        stock = _find_stock_coordinator(hass)
        if stock is not None:
            for item in stock.items:
                if item.get(CONF_ITEM_ID) == item_id:
                    filter_type = item.get(CONF_ITEM_TYPE)
                    break

    bindings: list[dict[str, Any]] = []
    for coord in _type_coordinators(hass):
        if filter_type and coord.cons_type != filter_type:
            continue
        triggered = set(coord.triggered_entities())
        for snapshot in coord.source_snapshots:
            sid = snapshot.get("entity_id")
            if entity_id and sid != entity_id:
                continue
            # 回显绑定的具体耗材（型号 / 名称）
            cid = snapshot.get("consumable_id")
            cmodel = None
            cname = None
            if cid:
                consumable = library.get(cid)
                if consumable is not None:
                    cmodel = consumable.model
                    cname = consumable.display_name(hass.config.language)
            bindings.append(
                {
                    "entry_type": coord.cons_type,
                    "entity_id": sid,
                    "consumable_id": cid,
                    "consumable_model": cmodel,
                    "consumable_name": cname,
                    "device_name": snapshot.get("device_name"),
                    "device_model": snapshot.get("device_model"),
                    "manufacturer": snapshot.get("manufacturer"),
                    "triggered": sid in triggered,
                }
            )

    result: dict[str, Any] = {"bindings": bindings}
    # 绑定前查询：返回实体设备信息 + 库内推荐耗材，形成「查→绑」闭环
    if entity_id:
        entity_info = _resolve_entity_info(hass, entity_id)
        result["entity_info"] = entity_info
        suggested: list[dict[str, Any]] = []
        manufacturer = entity_info.get("manufacturer")
        model = entity_info.get("model")
        if model:
            for consumable in library.find_compatible(manufacturer, model):
                suggested.append(
                    {
                        "id": consumable.id,
                        "type": consumable.cons_type,
                        "model": consumable.model,
                        "name": consumable.display_name(
                            hass.config.language
                        ),
                    }
                )
        result["suggested"] = suggested
    return result

async def async_unbind_entity(hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """解除实体到耗材的绑定（误绑时移除）。"""
    entity_id = call.data.get("entity_id")
    if not entity_id:
        raise ServiceValidationError("缺少 entity_id")

    removed: list[dict[str, Any]] = []
    for coord in _type_coordinators(hass):
        existing = coord.source_snapshots
        if not any(
            s.get("entity_id") == entity_id for s in existing
        ):
            continue
        # 记录被摘除条目绑定的耗材（用于回执），随后整体过滤掉该实体
        bound_cid = next(
            (s.get("consumable_id")
             for s in existing
             if s.get("entity_id") == entity_id),
            None,
        )
        options = dict(coord.options)
        options[CONF_SOURCE_ENTITIES] = [
            s for s in existing
            if s.get("entity_id") != entity_id
        ]
        hass.config_entries.async_update_entry(coord.entry, options=options)
        await coord.async_request_refresh()
        removed.append(
            {
                "entry_type": coord.cons_type,
                "consumable_id": bound_cid,
            }
        )
    if not removed:
        raise ServiceValidationError(
            f"未找到实体 {entity_id} 的绑定关系"
        )
    return {
        "entity_id": entity_id,
        "removed_from": removed,
        "removed_count": len(removed),
    }

# ---- 服务：添加耗材 / 设备映射（写入用户库，本地覆盖层） ----
def _to_str_list(value: Any) -> list[str]:
    """把服务入参规整为字符串数组：列表直接用，字符串按 JSON / 逗号解析。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except ValueError:
            pass
        return [
            x.strip()
            for x in value.replace("，", ",").split(",")
            if x.strip()
        ]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []

async def async_add_consumable(hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """按模板字段添加耗材到用户库（字段对齐 schema v1，ID 自动生成）。"""
    cons_type = call.data.get("cons_type")
    model = str(call.data.get("model") or "").strip()
    name = str(call.data.get("name") or "").strip()
    unit = str(call.data.get("unit") or "").strip()
    meta = call.data.get("meta") or {}
    if not (cons_type and model and name and unit):
        raise ServiceValidationError(
            "cons_type / model / name / unit 为必填字段"
        )
    if not isinstance(meta, dict):
        raise ServiceValidationError("meta 必须是对象（可为空 {}）")
    library = await async_load_library(hass)
    if cons_type not in library.types:
        raise ServiceValidationError(
            f"未知耗材类型 {cons_type}，支持：{', '.join(library.types)}"
        )
    try:
        cid = await async_write_user_consumable(
            hass, cons_type, model, name, unit, meta
        )
    except LibraryError as err:
        raise ServiceValidationError(str(err)) from err
    await _refresh_coordinator_libraries(hass)
    return {
        "consumable_id": cid,
        "added": {
            "id": cid,
            "type": cons_type,
            "model": model,
            "name": name,
            "unit": unit,
            "meta": meta,
        },
        "path": str(user_library_path(hass)),
    }

async def async_add_device( hass: HomeAssistant, call: ServiceCall ) -> dict[str, Any]:
    """添加设备-耗材映射到用户库（锚点重叠整条替换，引用完整性校验）。"""
    manufacturer = str(call.data.get("manufacturer") or "").strip()
    models = _to_str_list(call.data.get("models"))
    name = str(call.data.get("name") or "").strip()
    consumables = _to_str_list(call.data.get("consumables"))
    if not manufacturer:
        raise ServiceValidationError("缺少必填字段 manufacturer")
    if not models:
        raise ServiceValidationError("缺少必填字段 models（非空数组）")
    if not name:
        raise ServiceValidationError("缺少必填字段 name")
    if not consumables:
        raise ServiceValidationError("缺少必填字段 consumables（非空数组）")
    try:
        entry = await async_write_user_device(
            hass, manufacturer, models, name, consumables
        )
    except LibraryError as err:
        raise ServiceValidationError(str(err)) from err
    await _refresh_coordinator_libraries(hass)
    return {"added": entry, "path": str(user_library_path(hass))}

async def async_add_type( hass: HomeAssistant, call: ServiceCall ) -> dict[str, Any]:
    """添加自定义类型到用户库（新建语义，禁止覆盖；meta 需通过 parse_type 校验）。"""
    key = str(call.data.get("type_key") or "").strip().lower()
    name = str(call.data.get("name") or "").strip()
    icon = str(call.data.get("icon") or "").strip() or "mdi:package-variant"
    threshold_type = call.data.get("default_threshold_type")
    threshold = call.data.get("default_threshold")
    threshold_unit = str(call.data.get("default_threshold_unit") or "").strip()

    if not key:
        raise ServiceValidationError("缺少必填字段 type_key")
    if not ID_PATTERN.match(key):
        raise ServiceValidationError(
            "type_key 仅允许小写字母、数字与下划线"
        )
    if not name:
        raise ServiceValidationError("缺少必填字段 name")
    if threshold_type not in THRESHOLD_TYPES:
        raise ServiceValidationError(
            f"非法 default_threshold_type {threshold_type!r}，"
            f"支持：{', '.join(THRESHOLD_TYPES)}"
        )
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or threshold < 0
    ):
        raise ServiceValidationError("default_threshold 必须是非负数值")
    if threshold_unit not in THRESHOLD_UNIT_OPTIONS:
        raise ServiceValidationError(
            f"非法 default_threshold_unit {threshold_unit!r}，"
            f"支持：{', '.join(THRESHOLD_UNIT_OPTIONS)}"
        )
    library = await async_load_library(hass)
    if key in library.types:
        raise ServiceValidationError(
            f"类型键 {key} 已存在（新建语义，禁止覆盖已有类型）"
        )
    meta = {
        "name": name,
        "icon": icon,
        "default_threshold_type": threshold_type,
        "default_threshold": float(threshold),
        "default_threshold_unit": threshold_unit,
    }
    await async_write_user_type(hass, key, meta)
    await _refresh_coordinator_libraries(hass)
    return {"type_key": key, "added": meta, "path": str(user_library_path(hass))}

# ---- 服务：数据提取 ----
async def async_extract( hass: HomeAssistant, call: ServiceCall ) -> dict[str, Any]:
    """提取条目的结构化数据（include 可指定数据项，缺省全部）。"""
    include = call.data.get("include") or ["stock", "consumable_types"]
    result: dict[str, Any] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        # getattr 防御：加载过程中部分条目尚未写入 runtime_data
        data = getattr(entry, "runtime_data", None)
        if not isinstance(data, ConsumableManagerData):
            continue
        coord = data.coordinator
        if isinstance(coord, StockCoordinator) and "stock" in include:
            result["stock"] = {
                "entry_type": ENTRY_TYPE_STOCK,
                "status": coord.stock_status,
                "items": [
                    {
                        "item_id": item[CONF_ITEM_ID],
                        "item_name": item.get(CONF_ITEM_NAME),
                        "item_type": item.get(CONF_ITEM_TYPE),
                        "consumable_id": item.get(CONF_CONSUMABLE_ID),
                        "unit": coord.unit(item[CONF_ITEM_ID]),
                        "quantity": coord.quantity(item[CONF_ITEM_ID]),
                        "stock_threshold": coord.stock_threshold(
                            item[CONF_ITEM_ID]
                        ),
                        "low_stock": coord.is_low(item[CONF_ITEM_ID]),
                    }
                    for item in coord.items
                ],
            }
        elif (
            isinstance(coord, ConsumableTypeCoordinator)
            and "consumable_types" in include
        ):
            result.setdefault("consumable_types", []).append(
                {
                    "consumable_type": coord.cons_type,
                    "title": coord.title,
                    "source_entities": coord.source_entities,
                    "triggered_entities": coord.triggered_entities(),
                    "threshold": {
                        "type": coord.threshold_type,
                        "value": coord.threshold,
                        "unit": coord.threshold_unit,
                        "operator": coord.threshold_operator,
                    },
                    "replace_status": coord.replace_status,
                    "last_replaced": coord.last_replaced,
                }
            )
    return result

# ---- 服务：库存调整 / 标记更换 ----
async def _adjust_stock(hass: HomeAssistant,
    call: ServiceCall,
    delta: int,
) -> dict[str, Any]:
    coord = _find_stock_coordinator(hass)
    if coord is None:
        raise ServiceValidationError("未配置库存条目")
    item_id = _coerce_item_id(hass, call.data.get("item"))
    if not item_id:
        raise ServiceValidationError("请选择库存项")
    if not coord.item(item_id):
        raise ServiceValidationError(f"未找到库存项 {item_id}")
    coord.async_add_quantity(item_id, delta)
    await coord.async_request_refresh()
    return {"item_id": item_id, "quantity": coord.quantity(item_id)}

async def async_adjust_stock(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """调整库存：action 为 consume 时减少，否则增加。"""
    action = call.data.get("action", "add")
    quantity = int(call.data.get("quantity", 0))
    delta = -quantity if action == "consume" else quantity
    return await _adjust_stock(hass, call, delta)

# ---- 注册 ----
_SERVICES: tuple[tuple[str, Any, SupportsResponse], ...] = (
    ("bind_entity", async_bind_entity, SupportsResponse.OPTIONAL),
    ("unbind_entity", async_unbind_entity, SupportsResponse.OPTIONAL),
    ("query_binding", async_query_binding, SupportsResponse.OPTIONAL),
    ("add_consumable", async_add_consumable, SupportsResponse.OPTIONAL),
    ("add_device", async_add_device, SupportsResponse.OPTIONAL),
    ("add_type", async_add_type, SupportsResponse.OPTIONAL),
    ("extract", async_extract, SupportsResponse.ONLY),
    ("adjust_stock", async_adjust_stock, SupportsResponse.OPTIONAL),
)

async def async_setup_services(hass: HomeAssistant) -> None:
    """注册全部服务（schema 由 services.yaml 驱动，custom_value 手输自定义类型）。"""
    for name, func, supports_response in _SERVICES:
        # HA 调用处理函数只传 call 单参；闭包绑定 hass 适配 (hass, call) 签名。
        async def _handler(call: ServiceCall, _func: Any = func) -> Any:
            return await _func(hass, call)

        hass.services.async_register(
            DOMAIN,
            name,
            _handler,
            supports_response=supports_response,
        )
