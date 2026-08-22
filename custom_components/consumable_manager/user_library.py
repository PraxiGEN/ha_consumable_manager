"""耗材管理器 用户库平台（单文件覆盖层，与内置库合并后供全局消费）。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import LOGGER
from .library import (
    SCHEMA_VERSION,
    Consumable,
    DeviceMapping,
    Library,
    LibraryError,
    TypeMeta,
    load_library,
    parse_consumable,
    parse_device,
    parse_type,
)

# 用户库目录（位于 HA 配置目录下，隐藏目录）
USER_LIBRARY_DIR = ".consumable_manager"
USER_LIBRARY_FILE = "user_library.json"

def user_library_path(hass: HomeAssistant) -> Path:
    """用户库文件绝对路径（不保证存在）。"""
    return Path(hass.config.path(USER_LIBRARY_DIR, USER_LIBRARY_FILE))

@dataclass(frozen=True)
class UserLibraryData:
    """解析并通过校验的用户库三段数据。"""

    types: tuple[TypeMeta, ...] = ()
    consumables: tuple[Consumable, ...] = ()
    devices: tuple[DeviceMapping, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.types or self.consumables or self.devices)

def _device_anchors(dev: DeviceMapping) -> frozenset[tuple[str, str]]:
    """设备条目的锚点集合：(manufacturer, model) 组合，均忽略大小写。"""
    manufacturer = dev.manufacturer.strip().lower()
    return frozenset(
        (manufacturer, model.strip().lower())
        for model in dev.models
        if model.strip()
    )

def read_user_library(path: Path, builtin: Library) -> UserLibraryData:
    """读取并校验用户库（不含降级逻辑，校验失败抛 LibraryError）。

    引用完整性以「内置库 + 用户库」合并后的口径校验：
    用户耗材可引用内置类型，用户设备可引用内置耗材 id。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LibraryError(f"{path}: 应为 JSON 对象")

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise LibraryError(
            f"{path}: schema_version={version} 不受支持（当前支持 {SCHEMA_VERSION}）"
        )

    raw_types = data.get("types") or {}
    raw_consumables = data.get("consumables") or []
    raw_devices = data.get("devices") or []
    if not isinstance(raw_types, dict):
        raise LibraryError(f"{path}: types 应为对象（类型键 → 类型元数据）")
    if not isinstance(raw_consumables, list):
        raise LibraryError(f"{path}: consumables 应为数组")
    if not isinstance(raw_devices, list):
        raise LibraryError(f"{path}: devices 应为数组")

    # 1. 类型：用户类型可与内置类型同键（整条替换），条目字段全必填
    user_types = tuple(
        parse_type(key, raw, path) for key, raw in raw_types.items()
    )
    known_types: dict[str, TypeMeta] = {
        meta.key: meta for meta in builtin.type_metas
    }
    known_types.update({meta.key: meta for meta in user_types})

    # 2. 耗材：id 与内置同值即整条替换；文件内 id 不得重复
    user_consumables: list[Consumable] = []
    seen_ids: set[str] = set()
    for raw in raw_consumables:
        item = parse_consumable(raw, path, known_types)
        if item.id in seen_ids:
            raise LibraryError(f"{path}: 用户库耗材 id 重复: {item.id}")
        seen_ids.add(item.id)
        user_consumables.append(item)

    # 3. 设备：可引用内置耗材；锚点（manufacturer, model）文件内不得重叠
    known_ids = {c.id for c in builtin.consumables} | seen_ids
    user_devices: list[DeviceMapping] = []
    seen_anchors: set[tuple[str, str]] = set()
    for raw in raw_devices:
        dev = parse_device(raw, path, known_ids)
        anchors = _device_anchors(dev)
        if anchors & seen_anchors:
            raise LibraryError(
                f"{path}: 用户库设备锚点重叠（manufacturer+model 重复）: "
                f"{dev.manufacturer} {dev.models[0]}"
            )
        seen_anchors |= anchors
        user_devices.append(dev)

    return UserLibraryData(
        types=user_types,
        consumables=tuple(user_consumables),
        devices=tuple(user_devices),
    )

def merge_library(builtin: Library, user: UserLibraryData) -> Library:
    """合并内置库与用户库：同锚点用户优先、整条替换、不删除。

    纯装配逻辑（不抛错）：输入应已通过 read_user_library 校验。
    合并结果保持内置条目顺序，用户覆盖原位替换、新增条目追加。
    """
    # 类型：同键整条替换
    types: dict[str, TypeMeta] = {
        meta.key: meta for meta in builtin.type_metas
    }
    types.update({meta.key: meta for meta in user.types})

    # 耗材：同 id 整条替换
    consumables: dict[str, Consumable] = {
        c.id: c for c in builtin.consumables
    }
    consumables.update({c.id: c for c in user.consumables})

    # 设备：内置条目锚点与任一用户条目重叠即整条丢弃，由用户条目接管
    user_anchors: set[tuple[str, str]] = set()
    for dev in user.devices:
        user_anchors |= _device_anchors(dev)
    devices: list[DeviceMapping] = [
        dev
        for dev in builtin.all_devices()
        if not (_device_anchors(dev) & user_anchors)
    ]
    devices.extend(user.devices)

    return Library(
        type_metas=tuple(types.values()),
        consumables=tuple(consumables.values()),
        devices=tuple(devices),
    )

def load_merged_library(
    base_dir: Path | None = None,
    user_path: Path | None = None,
) -> Library:
    """加载内置库并合并用户库（同步，供 executor 调用）。

    用户库文件缺失 → 直接返回内置库；
    用户库坏文件 → 整体忽略 + 警告日志，回退内置库（绝不因用户手改出错而拖垮集成）。
    """
    builtin = load_library(base_dir)
    if user_path is None or not Path(user_path).is_file():
        return builtin
    try:
        user = read_user_library(Path(user_path), builtin)
    except (LibraryError, ValueError, OSError) as err:
        # ValueError 覆盖 json.JSONDecodeError；OSError 覆盖读盘失败
        LOGGER.warning(
            "用户库 %s 校验失败，已整体忽略并回退内置库：%s", user_path, err
        )
        return builtin
    if user.is_empty:
        return builtin
    return merge_library(builtin, user)

async def async_load_library(hass: HomeAssistant) -> Library:
    """异步入口：executor 中加载内置库 + 用户库合并结果。

    全集成统一的库加载入口（__init__ / config_flow / services / sensor），
    消费方一律经由本函数取库，不再直接读内置库。
    """
    user_path = user_library_path(hass)
    return await hass.async_add_executor_job(
        load_merged_library, None, user_path
    )

def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写盘：写临时文件后 rename，避免半截文件损坏用户库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)

def _load_user_data(path: Path) -> dict[str, Any]:
    """读取用户库为三段字典；缺失 / 损坏则重建为空三段。"""
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    data.setdefault("types", {})
    data.setdefault("consumables", [])
    data.setdefault("devices", [])
    # 写库路径强制修正版本：损坏内容本就被降级忽略，但写出文件必须可读
    data["schema_version"] = SCHEMA_VERSION
    return data

def _consumable_id(cons_type: str, model: str, name: str) -> str:
    """由类型 + 型号（缺省名称）推导稳定耗材 id（型号非 ASCII 退化内容哈希）。"""
    base = (model or name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    if not slug:
        slug = hashlib.sha1(
            f"{cons_type}:{model}:{name}".encode("utf-8")
        ).hexdigest()[:8]
    return f"{cons_type}_{slug}"

def write_user_type(path: Path, key: str, meta: dict[str, Any]) -> None:
    """把一条类型元数据写入用户库 types 段（同键整条替换）。"""
    data = _load_user_data(path)
    data["types"][key] = meta
    _atomic_write_json(path, data)

async def async_write_user_type(hass: HomeAssistant,
    key: str,
    meta: dict[str, Any],
) -> None:
    """异步入口：在 executor 中把类型元数据写入用户库。"""
    path = user_library_path(hass)
    await hass.async_add_executor_job(write_user_type, path, key, meta)

def _unique_cid(cid: str,
    cons_type: str,
    model: str,
    name: str,
    unit: str,
    taken: set[str],
) -> str:
    """ID 冲突兜底：追加内容短哈希直至唯一（同输入幂等）。"""
    digest = hashlib.sha1(
        f"{cons_type}:{model}:{name}:{unit}".encode("utf-8")
    ).hexdigest()[:6]
    candidate = f"{cid}_{digest}"
    while candidate in taken:
        digest = hashlib.sha1(
            f"{candidate}:{digest}".encode("utf-8")
        ).hexdigest()[:6]
        candidate = f"{cid}_{digest}"
    return candidate

def write_user_consumable(
    path: Path,
    cons_type: str,
    model: str,
    name: str,
    unit: str,
    meta: dict[str, Any] | None = None,
) -> str:
    """写入耗材到用户库（同 id 整条替换），返回生效 id。"""
    model = (model or "").strip()
    name = (name or "").strip()
    unit = (unit or "").strip()
    for label, value in (
        ("型号 model", model),
        ("名称 name", name),
        ("单位 unit", unit),
    ):
        if not value:
            raise LibraryError(
                f"自定义耗材缺少必填字段 {label}，无法写入用户库"
            )
    cid = _consumable_id(cons_type, model, name)
    builtin = load_library()
    data = _load_user_data(path)
    # type 必填且必须已定义（内置库 + 用户库类型全集）
    if cons_type not in {t.key for t in builtin.type_metas} and (
        cons_type not in data["types"]
    ):
        raise LibraryError(f"未知耗材类型 {cons_type}，无法写入用户库")

    entry = {
        "id": cid,
        "type": cons_type,
        "model": model,
        "name": name,
        "unit": unit,
        "meta": meta or {},
    }
    # 唯一性兜底（先内置后用户库；同输入始终收敛到同一 id）
    builtin_by_id = {c.id: c for c in builtin.consumables}
    if cid in builtin_by_id:
        b = builtin_by_id[cid]
        if b.cons_type == cons_type and b.model.lower() == model.lower():
            return cid  # 同类型同型号 → 链接内置，不写用户库
        cid = _unique_cid(
            cid, cons_type, model, name, unit, set(builtin_by_id)
        )
        entry["id"] = cid
    else:
        user_by_id = {
            c.get("id"): c
            for c in data["consumables"]
            if isinstance(c.get("id"), str)
        }
        if cid in user_by_id:
            cur = user_by_id[cid]
            # 同类型同型号（忽略大小写）→ 同一耗材，幂等覆盖更新；
            # 型号不同仅 slug 巧合 → 追加短哈希错开（防御性分支）
            same_model = (
                cur.get("type") == cons_type
                and isinstance(cur.get("model"), str)
                and cur.get("model").strip().lower() == model.lower()
            )
            if not same_model:
                cid = _unique_cid(
                    cid, cons_type, model, name, unit,
                    set(builtin_by_id) | set(user_by_id),
                )
                entry["id"] = cid

    consumables = list(data["consumables"])
    for i, c in enumerate(consumables):
        if c.get("id") == cid:
            consumables[i] = entry
            break
    else:
        consumables.append(entry)
    data["consumables"] = consumables
    _atomic_write_json(path, data)
    return cid

async def async_write_user_consumable(
    hass: HomeAssistant,
    cons_type: str,
    model: str,
    name: str,
    unit: str,
    meta: dict[str, Any] | None = None,
) -> str:
    """异步入口：在 executor 中把耗材写入用户库，返回生效 id。"""
    path = user_library_path(hass)
    return await hass.async_add_executor_job(
        write_user_consumable, path, cons_type, model, name, unit, meta
    )

def _raw_device_anchors(entry: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """原始 dict 设备条目的锚点集合：(manufacturer, model) 组合，忽略大小写。"""
    manufacturer = str(entry.get("manufacturer", "")).strip().lower()
    anchors: set[tuple[str, str]] = set()
    for m in entry.get("models") or []:
        if isinstance(m, str) and m.strip():
            anchors.add((manufacturer, m.strip().lower()))
    return frozenset(anchors)

def write_user_device(
    path: Path,
    manufacturer: str,
    models: list[str],
    name: str,
    consumables: list[str],
) -> dict[str, Any]:
    """写入设备映射到用户库（锚点重叠整条替换），返回写入条目。"""
    manufacturer = (manufacturer or "").strip()
    models = list(
        dict.fromkeys(
            m.strip() for m in (models or []) if isinstance(m, str) and m.strip()
        )
    )
    name = (name or "").strip()
    consumables = list(
        dict.fromkeys(
            c.strip()
            for c in (consumables or [])
            if isinstance(c, str) and c.strip()
        )
    )
    for label, value in (
        ("制造商 manufacturer", manufacturer),
        ("型号数组 models", models),
        ("名称 name", name),
        ("耗材列表 consumables", consumables),
    ):
        if not value:
            raise LibraryError(
                f"自定义设备缺少必填字段 {label}，无法写入用户库"
            )
    # 引用完整性：consumables 必须存在于「内置库 + 用户库」合并 id 集
    builtin = load_library()
    data = _load_user_data(path)
    known_ids: set[str] = {c.id for c in builtin.consumables}
    known_ids.update(
        c.get("id")
        for c in data["consumables"]
        if isinstance(c, dict) and isinstance(c.get("id"), str)
    )
    for cid in consumables:
        if cid not in known_ids:
            raise LibraryError(f"未知耗材 {cid}，无法写入用户库设备映射")

    entry = {
        "manufacturer": manufacturer,
        "models": models,
        "name": name,
        "consumables": consumables,
    }
    new_anchors = {(manufacturer.lower(), m.lower()) for m in models}
    devices = [
        dev
        for dev in data["devices"]
        if not isinstance(dev, dict)
        or not (_raw_device_anchors(dev) & new_anchors)
    ]
    devices.append(entry)
    data["devices"] = devices
    _atomic_write_json(path, data)
    return entry

async def async_write_user_device(
    hass: HomeAssistant,
    manufacturer: str,
    models: list[str],
    name: str,
    consumables: list[str],
) -> dict[str, Any]:
    """异步入口：在 executor 中把设备映射写入用户库，返回写入的条目。"""
    path = user_library_path(hass)
    return await hass.async_add_executor_job(
        write_user_device, path, manufacturer, models, name, consumables
    )
