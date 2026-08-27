"""耗材管理器 耗材库平台。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .const import LOGGER, THRESHOLD_TYPES

# index.json / 用户库 当前支持的 schema 版本（v1：多文件零目录结构，集成首个发布版本）
SCHEMA_VERSION = 1
# 耗材 id 约定：<type>_<slug>，小写字母/数字/下划线
ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
# name 多语言回退链：精确 locale → 英文(en) → 中文(zh-Hans)。
# 即「中文显示中文，其他语言默认英文」，整集成统一（display_name 各处共用）。
LOCALE_FALLBACKS: tuple[str, ...] = ("en", "zh-Hans")
# 多语言字段原始形态：plain 字符串或 locale → 文案 映射
LocalizedText = str | dict[str, str]
_LIBRARY_DIR = Path(__file__).resolve().parent / "library"

def resolve_name(value: LocalizedText,
    locale: str | None,
    fallback: str,
    names: dict[str, str] | None = None,
) -> str:
    """按回退链解析多语言字段：names 映射 → 数据内 name → 兜底。"""
    if names:
        for key in (locale, *LOCALE_FALLBACKS):
            if key and (text := names.get(key)):
                return text
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        for key in (locale, *LOCALE_FALLBACKS):
            if key and (text := value.get(key)):
                return text
    return fallback

@dataclass(frozen=True)
class TypeMeta:
    """耗材类型元数据（来自 index.json 的 types 表）。"""

    key: str
    name: LocalizedText
    icon: str
    default_threshold_type: str
    default_threshold: float
    default_threshold_unit: str
    names: dict[str, str] | None = None

    def display_name(self, locale: str | None = None) -> str:
        return resolve_name(self.name, locale, fallback=self.key, names=self.names)

@dataclass(frozen=True)
class Consumable:
    """耗材定义（来自 consumables.json）。"""

    cons_type: str
    id: str
    model: str
    name: LocalizedText
    unit: str
    meta: dict[str, Any] = field(default_factory=dict)
    names: dict[str, str] | None = None

    def display_name(self, locale: str | None = None) -> str:
        return resolve_name(
            self.name, locale, fallback=self.model, names=self.names
        )

class LibraryError(ValueError):
    """耗材库加载或校验失败。"""

class Library:
    """装配完成的耗材库，提供双向查询接口。"""

    def __init__(
        self,
        type_metas: tuple[TypeMeta, ...],
        consumables: tuple[Consumable, ...],
    ) -> None:
        self._type_metas = type_metas
        self._types: dict[str, TypeMeta] = {t.key: t for t in type_metas}
        self._consumables = consumables
        self._by_id: dict[str, Consumable] = {c.id: c for c in consumables}
        self._by_type: dict[str, tuple[Consumable, ...]] = {}
        for c in consumables:
            self._by_type.setdefault(c.cons_type, []).append(c)
        self._by_type = {t: tuple(v) for t, v in self._by_type.items()}

    # ---- 类型 ----
    @property
    def types(self) -> tuple[str, ...]:
        """已加载的耗材类型键列表。"""
        return tuple(self._types)

    @property
    def type_metas(self) -> tuple[TypeMeta, ...]:
        """全部类型元数据（含重复覆盖后的最终结果）。"""
        return self._type_metas

    @property
    def consumables(self) -> tuple[Consumable, ...]:
        """全部耗材扁平视图（供用户库合并读取）。"""
        return self._consumables

    def type_meta(self, key: str) -> TypeMeta | None:
        """按类型键取元数据（不存在返回 None，如自定义类型）。"""
        return self._types.get(key)

    def type_icon(self, key: str) -> str | None:
        meta = self._types.get(key)
        return meta.icon if meta else None

    # ---- 正查：耗材 ----
    @property
    def count(self) -> int:
        return len(self._consumables)

    def by_type(self, cons_type: str) -> tuple[Consumable, ...]:
        """按类型返回全部耗材。"""
        return self._by_type.get(cons_type, ())

    def get(self, item_id: str) -> Consumable | None:
        """按全局唯一 id 精确查找。"""
        return self._by_id.get(item_id)

    def find_by_text(self, text: str) -> list[Consumable]:
        """跨类型文本搜索：匹配型号、name 与 names 映射的全部语言形态。"""
        needle = text.strip().lower()
        if not needle:
            return []
        result: list[Consumable] = []
        for item in self._consumables:
            haystack = [item.model]
            if isinstance(item.name, str):
                haystack.append(item.name)
            elif isinstance(item.name, dict):
                haystack.extend(item.name.values())
            if item.names:
                haystack.extend(item.names.values())
            if any(needle in value.lower() for value in haystack):
                result.append(item)
        return result

# ---- 校验辅助（公共：用户库复用同一套逐字段校验）----
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LibraryError(message)

def _validate_name( value: Any, path: Path, label: str ) -> LocalizedText:
    """name 字段：plain 非空字符串，或 locale→非空字符串 的非空映射。"""
    if isinstance(value, str) and value.strip():
        return value
    _require(
        isinstance(value, dict) and value,
        f"{path}: {label} 的 name 必须是非空字符串或非空 locale 映射",
    )
    for locale, text in value.items():
        _require(
            isinstance(locale, str) and locale.strip(),
            f"{path}: {label} 的 name 含空 locale 键",
        )
        _require(
            isinstance(text, str) and text.strip(),
            f"{path}: {label} 的 name.{locale} 必须是非空字符串",
        )
    return value

def parse_type(
    key: str,
    raw: dict[str, Any],
    path: Path,
    names: dict[str, dict[str, str]] | None = None,
) -> TypeMeta:
    name = _validate_name(raw.get("name"), path, f"类型 {key}")
    icon = raw.get("icon")
    _require(
        isinstance(icon, str) and icon.strip(),
        f"{path}: 类型 {key} 缺少 icon",
    )
    threshold_type = raw.get("default_threshold_type")
    _require(
        threshold_type in THRESHOLD_TYPES,
        f"{path}: 类型 {key} 的 default_threshold_type 非法: {threshold_type}",
    )
    threshold = raw.get("default_threshold")
    _require(
        isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
        and threshold >= 0,
        f"{path}: 类型 {key} 的 default_threshold 必须是非负数值",
    )
    unit = raw.get("default_threshold_unit")
    _require(
        isinstance(unit, str) and unit.strip(),
        f"{path}: 类型 {key} 缺少 default_threshold_unit",
    )
    return TypeMeta(
        key=key,
        name=name,
        icon=icon,
        default_threshold_type=threshold_type,
        default_threshold=threshold,
        default_threshold_unit=unit,
        names=(names or {}).get(key),
    )

def parse_consumable(
    raw: dict[str, Any],
    path: Path,
    known_types: dict[str, TypeMeta],
    names: dict[str, dict[str, str]] | None = None,
) -> Consumable:
    item_id = raw.get("id")
    _require(
        isinstance(item_id, str) and ID_PATTERN.match(item_id),
        f"{path}: 条目 id 非法（约定 <type>_<slug>，小写字母/数字/下划线）: {item_id}",
    )
    cons_type = raw.get("type")
    _require(
        cons_type in known_types,
        f"{path}: 条目 {item_id} 的 type 未在 index.json types 中定义: {cons_type}",
    )
    model = raw.get("model")
    _require(
        isinstance(model, str) and model.strip(),
        f"{path}: 条目 {item_id} 缺少 model",
    )
    name = _validate_name(raw.get("name"), path, f"条目 {item_id}")
    unit = raw.get("unit")
    _require(
        isinstance(unit, str) and unit.strip(),
        f"{path}: 条目 {item_id} 缺少 unit",
    )
    meta = raw.get("meta")
    _require(
        isinstance(meta, dict),
        f"{path}: 条目 {item_id} 缺少 meta（可为空对象 {{}}）",
    )
    return Consumable(
        cons_type=cons_type,
        id=item_id,
        model=model,
        name=name,
        unit=unit,
        meta=dict(meta),
        names=(names or {}).get(item_id),
    )

def _load_names_table(root: Path) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    """加载 names.json 多语言映射表（types / consumables）；缺失 / 损坏时降级为空表并告警。"""
    names_path = root / "names.json"
    if not names_path.is_file():
        return {}, {}
    try:
        raw = json.loads(names_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("names.json 解析失败，已降级为数据内 name")
        return {}, {}
    if not isinstance(raw, dict):
        LOGGER.warning("names.json 结构非法，已降级为数据内 name")
        return {}, {}
    return (
        raw.get("types", {}) if isinstance(raw.get("types"), dict) else {},
        raw.get("consumables", {})
        if isinstance(raw.get("consumables"), dict)
        else {},
    )

def load_library(base_dir: Path | None = None) -> Library:
    """加载并校验 library/ 目录，返回装配完成的 Library。"""
    root = Path(base_dir) if base_dir is not None else _LIBRARY_DIR
    index_path = root / "index.json"
    _require(index_path.is_file(), f"耗材库缺少 index.json: {index_path}")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    schema_version = index.get("schema_version")
    _require(
        schema_version == SCHEMA_VERSION,
        f"耗材库 schema_version={schema_version} 不受支持（当前支持 {SCHEMA_VERSION}）",
    )

    # 多语言映射表（可选附属文件）
    type_names, consumable_names = _load_names_table(root)

    # 类型元数据表
    raw_types = index.get("types")
    _require(
        isinstance(raw_types, dict) and raw_types,
        f"{index_path}: 缺少 types 类型元数据表",
    )
    type_metas = tuple(
        parse_type(key, raw, index_path, type_names)
        for key, raw in raw_types.items()
    )
    known_types = {t.key: t for t in type_metas}

    # 耗材扁平数组
    consumables_path = root / "consumables.json"
    _require(
        consumables_path.is_file(), f"耗材库缺少 consumables.json: {consumables_path}"
    )
    raw_consumables = json.loads(
        consumables_path.read_text(encoding="utf-8")
    )
    _require(
        isinstance(raw_consumables, list),
        f"{consumables_path}: 应为 JSON 数组",
    )
    consumables: list[Consumable] = []
    seen_ids: set[str] = set()
    for raw in raw_consumables:
        item = parse_consumable(raw, consumables_path, known_types, consumable_names)
        _require(
            item.id not in seen_ids,
            f"耗材 id 重复: {item.id}（{consumables_path}）",
        )
        seen_ids.add(item.id)
        consumables.append(item)

    return Library(type_metas, tuple(consumables))