"""耗材管理器 服务平台。"""
from __future__ import annotations

import copy
import json
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import SupportsResponse

from .const import (
    DOMAIN, CONF_BINDING_GROUPS, CONF_CONSUMABLE_ID, CONF_GROUP_ID,
    CONF_GROUP_NAME, CONF_ITEM_ID, CONF_ITEM_NAME, CONF_ITEM_TYPE,
    CONF_SOURCE_ENTITIES, CONF_STOCK_ITEMS,
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

def _resolve_group_id(
    hass: HomeAssistant, value: str | None
) -> str | None:
    """分组选择框的值规整为 group_id。"""
    if not value:
        return None
    if "." not in value:
        return value
    reg = er.async_get(hass)
    reg_entry = reg.async_get(value)
    if reg_entry is None or not reg_entry.unique_id:
        return None
    uid = reg_entry.unique_id
    for coord in _type_coordinators(hass):
        eid = coord.entry.entry_id
        for g in coord.groups:
            gid = g.get(CONF_GROUP_ID)
            if gid is None:
                continue
            if uid in (f"{eid}_grp_{gid}", f"{eid}_grpdata_{gid}"):
                return gid
    return None

# ---- 服务：实体耗材绑定 ----
async def _resolve_consumable(
    hass: HomeAssistant,
    library: Library,
    consumable_id: str | None,
) -> tuple[Consumable, str]:
    """解析目标耗材：必须显式指定 consumable_id（或在调用前由关联库存项继承）。"""
    if not consumable_id:
        raise ServiceValidationError(
            "未指定 consumable_id，请在绑定时显式选择耗材或关联一个库存项"
        )
    consumable = library.get(consumable_id)
    if consumable is None:
        raise ServiceValidationError(
            f"耗材库中不存在耗材 {consumable_id}"
        )
    return consumable, "manual"

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
    """绑定实体到耗材（显式指定 consumable_id 或继承关联库存项），可选关联库存项。"""
    entity_id = call.data.get("entity_id")
    consumable_id = call.data.get("consumable_id")
    item_id = _coerce_item_id(hass, call.data.get("item"))
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
        hass, library, consumable_id
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
    groups = copy.deepcopy(coord.groups)
    new_snap = build_source_snapshots(hass, [entity_id])[0]
    new_snap["consumable_id"] = consumable.id
    # 目标分组：显式 group_id 优先；否则首个分组（无则建默认组）
    target_gid = call.data.get("group_id")
    target = (
        next((g for g in groups if g.get(CONF_GROUP_ID) == target_gid), None)
        if target_gid
        else None
    )
    if target is None:
        target = (
            groups[0]
            if groups
            else {
                CONF_GROUP_ID: "default",
                CONF_GROUP_NAME: coord.title or "默认",
                CONF_SOURCE_ENTITIES: [],
            }
        )
        if not groups:
            groups.append(target)
    # 在目标分组内更新或追加快照（避免改到条目原始 options）
    snaps = target.setdefault(CONF_SOURCE_ENTITIES, [])
    found = False
    for s in snaps:
        if s.get("entity_id") == entity_id:
            s["consumable_id"] = consumable.id
            found = True
            break
    if not found:
        snaps.append(new_snap)
    options[CONF_BINDING_GROUPS] = groups
    options.pop(CONF_SOURCE_ENTITIES, None)
    hass.config_entries.async_update_entry(coord.entry, options=options)
    # 纯映射：写库即生效（HA 会因 options 变更重载条目），不再立即刷新。
    # 把新实体并入触发基线（内存 + 持久化），使重载后的新协调器首轮刷新
    # 差集为空 → 不发通知；越阈实体的「更换」待办由后续监控周期（重载/轮询）
    # 经 step0 自然产生，而非绑定动作的副作用。绑定服务本身不刷新 → 不生成待办/通知。
    coord.sync_alert_baseline()
    coord._persist_alert_baseline(coord._compute_triggered())

    if item_id:
        _link_stock_item(hass, item_id, consumable)

    return {
        "entity_id": entity_id,
        "consumable_id": consumable.id,
        "consumable_name": consumable.display_name(hass.config.language),
        "entry_type": coord.cons_type,
        "matched_by": matched_by,
        "item_id": item_id,
    }

async def _collect_bindings(
    hass: HomeAssistant,
    library: Library,
    entity_id: str | None = None,
    consumable_id: str | None = None,
    item_id: str | None = None,
) -> list[dict[str, Any]]:
    """收集实体↔耗材绑定映射（按实体 / 耗材 / 库存项过滤）。"""
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
        group_of = {
            s.get("entity_id"): g.get(CONF_GROUP_NAME)
            for g in coord.groups
            for s in g.get(CONF_SOURCE_ENTITIES, [])
        }
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
                    "group": group_of.get(sid),
                    "consumable_id": cid,
                    "consumable_model": cmodel,
                    "consumable_name": cname,
                    "triggered": sid in triggered,
                }
            )
    return bindings

async def async_query_binding(hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """查询绑定关系：按实体 / 耗材 / 库存项过滤。"""
    entity_id = call.data.get("entity_id")
    consumable_id = call.data.get("consumable_id")
    item_id = _coerce_item_id(hass, call.data.get("item"))
    library = await async_load_library(hass)
    bindings = await _collect_bindings(
        hass, library, entity_id, consumable_id, item_id
    )
    return {"bindings": bindings}

async def async_unbind_entity(hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """解除实体绑定的耗材：只清除 实体→consumable_id 这条映射，实体仍留分组。"""
    entity_id = call.data.get("entity_id")
    if not entity_id:
        raise ServiceValidationError("缺少 entity_id")

    removed: list[dict[str, Any]] = []
    did_unbind = False
    for coord in _type_coordinators(hass):
        groups = copy.deepcopy(coord.groups)
        if not any(
            s.get("entity_id") == entity_id
            for g in groups
            for s in g.get(CONF_SOURCE_ENTITIES, [])
        ):
            continue
        # 清空调该实体快照的 consumable_id（移除 实体→耗材 映射）；
        # 实体快照本身保留，分组不缩水、也不丢弃空分组。
        unbound_cid = None
        for g in groups:
            for s in g.get(CONF_SOURCE_ENTITIES, []):
                if s.get("entity_id") == entity_id and s.get(CONF_CONSUMABLE_ID):
                    unbound_cid = s[CONF_CONSUMABLE_ID]
                    s[CONF_CONSUMABLE_ID] = None
                    did_unbind = True
        options = dict(coord.options)
        options[CONF_BINDING_GROUPS] = groups
        options.pop(CONF_SOURCE_ENTITIES, None)
        hass.config_entries.async_update_entry(coord.entry, options=options)
        # 纯映射：解绑仅清除 实体→耗材 映射（实体仍留分组监控，触发态不变）。
        # 并入基线（内存 + 持久化）防止重载误报通知；不立即刷新 → 解绑动作
        # 本身不生成待办/通知。
        coord.sync_alert_baseline()
        coord._persist_alert_baseline(coord._compute_triggered())
        removed.append(
            {
                "entry_type": coord.cons_type,
                "consumable_id": unbound_cid,
            }
        )
    if not did_unbind:
        raise ServiceValidationError(
            f"未找到实体 {entity_id} 的耗材绑定关系"
        )
    return {
        "entity_id": entity_id,
        "unbound_from": removed,
        "unbound_count": len(removed),
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

# ---- 服务：查询数据（统一数据出口，必须指定 data_type）----
_DATA_TYPES = (
    "stock",        # 库存条目
    "type_entry",   # 耗材类型条目
    "group_data",   # 分组实体数据（成员含 consumable 字段）
    "types",        # 耗材类型元数据
    "consumables",  # 全部耗材
)

async def async_query_data(hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """查询本集成的各类数据（必须指定 data_type，按数据类型支持不同过滤）。"""
    data_type = call.data.get("data_type")
    if not data_type:
        raise ServiceValidationError(
            "必须指定 data_type（数据种类），可选：" + ", ".join(_DATA_TYPES)
        )
    if data_type not in _DATA_TYPES:
        raise ServiceValidationError(
            f"未知 data_type {data_type!r}，支持：" + ", ".join(_DATA_TYPES)
        )
    library = await async_load_library(hass)
    locale = hass.config.language
    consumable_type = call.data.get("consumable_type")
    entry_id = call.data.get("entry_id")
    group_id = _resolve_group_id(hass, call.data.get("group_entity"))
    triggered_only = bool(call.data.get("triggered_only"))

    if data_type == "stock":
        return {"stock": _query_stock(hass)}
    if data_type == "type_entry":
        return {"type_entries": _query_type_entries(hass)}
    if data_type == "group_data":
        return {"group_data": _query_group_data(
            hass, entry_id=entry_id, consumable_type=consumable_type,
            group_id=group_id, triggered_only=triggered_only)}
    if data_type == "types":
        return {"types": _query_types(library, locale)}
    if data_type == "consumables":
        return {"consumables": _query_consumables(
            library, locale, consumable_type=consumable_type)}
    return {}

def _query_stock(hass: HomeAssistant) -> list[dict[str, Any]]:
    """库存条目数据：逐个库存协调器，输出库存项 + 低库存状态。"""
    result: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = getattr(entry, "runtime_data", None)
        if not isinstance(data, ConsumableManagerData):
            continue
        coord = data.coordinator
        if isinstance(coord, StockCoordinator):
            result.append({
                "entry_id": entry.entry_id,
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
            })
    return result

def _query_type_entries(
    hass: HomeAssistant, entry_id: str | None = None
) -> list[dict[str, Any]]:
    """耗材类型条目数据：阈值 / 触发实体 / 分组等。"""
    result: list[dict[str, Any]] = []
    for coord in _type_coordinators(hass):
        if entry_id and coord.entry.entry_id != entry_id:
            continue
        result.append({
            "entry_id": coord.entry.entry_id,
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
            "groups": [
                {
                    "group_id": g.get(CONF_GROUP_ID),
                    "group_name": g.get(CONF_GROUP_NAME),
                }
                for g in coord.groups
            ],
        })
    return result

def _query_group_data(
    hass: HomeAssistant,
    entry_id: str | None = None,
    consumable_type: str | None = None,
    group_id: str | None = None,
    triggered_only: bool = False,
) -> list[dict[str, Any]]:
    """分组实体数据：每非自定义分组输出成员明细（含已绑定耗材名称）。"""
    result: list[dict[str, Any]] = []
    for coord in _type_coordinators(hass):
        if entry_id and coord.entry.entry_id != entry_id:
            continue
        if consumable_type and coord.cons_type != consumable_type:
            continue
        for group in coord.groups:
            if coord._group_is_custom(group):
                continue
            if group_id and group.get(CONF_GROUP_ID) != group_id:
                continue
            md = coord.group_member_data(group)
            # triggered_only：仅保留存在已触发成员的分组
            if triggered_only and not md.get("triggered_entities"):
                continue
            result.append({
                "entry_id": coord.entry.entry_id,
                "entry_type": coord.cons_type,
                "entry_title": coord.title,
                "group_id": group.get(CONF_GROUP_ID),
                "group": md.get("group"),
                "min_value": coord.group_min_value(group),
                "consumable_type": md.get("consumable_type"),
                "threshold_type": md.get("threshold_type"),
                "threshold": md.get("threshold"),
                "threshold_unit": md.get("threshold_unit"),
                "threshold_operator": md.get("threshold_operator"),
                "normal_entities": md.get("normal_entities"),
                "triggered_entities": md.get("triggered_entities"),
                "last_replaced": md.get("last_replaced"),
            })
    return result

def _query_types(
    library: Library, locale: str | None
) -> list[dict[str, Any]]:
    """耗材类型元数据列表。"""
    result: list[dict[str, Any]] = []
    for key in library.types:
        meta = library.type_meta(key)
        if meta is None:
            continue
        result.append({
            "type_key": key,
            "name": meta.display_name(locale),
            "icon": meta.icon,
            "default_threshold_type": meta.default_threshold_type,
            "default_threshold": meta.default_threshold,
            "default_threshold_unit": meta.default_threshold_unit,
        })
    return result

def _query_consumables(
    library: Library, locale: str | None,
    consumable_type: str | None = None,
) -> list[dict[str, Any]]:
    """全部耗材列表（含 id / 类型 / 型号 / 名称 / 单位 / meta）。"""
    consumables = library.consumables
    if consumable_type:
        consumables = [c for c in consumables if c.cons_type == consumable_type]
    return [
        {
            "consumable_id": c.id,
            "type": c.cons_type,
            "model": c.model,
            "name": c.display_name(locale),
            "unit": c.unit,
            "meta": c.meta,
        }
        for c in consumables
    ]

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
    ("add_type", async_add_type, SupportsResponse.OPTIONAL),
    ("query_data", async_query_data, SupportsResponse.ONLY),
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