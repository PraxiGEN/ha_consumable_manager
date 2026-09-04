#!/usr/bin/env python3
"""耗材管理器 摄入工具（贡献草稿 → 内置库，仅 CI 中运行，不在本地使用）。

只读取 git 分支中的 contributions/ 文件夹（contributions/<用户名>/
user_library.json），不读取任何本地路径。
用法：--check 校验内置库；--ingest 摄入全部草稿（锚点去重裁决、多语言
拆解进 names.json、排序归位、原子写）；--dry-run 只报告不写文件。
规则：同锚点同内容跳过、不同内容人工裁决；单向 草稿 → 内置库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# 定位集成包目录（工具仅在 CI 中运行，仓库根布局）：
#   <repo>/tools/ingest.py
#   <repo>/custom_components/consumable_manager/library/     内置库（写入目标）
#   <repo>/contributions/<用户名>/user_library.json          分支中的贡献草稿
_TOOLS_DIR = Path(__file__).resolve().parent

def _locate_package() -> Path:
    candidate = _TOOLS_DIR.parent / "custom_components" / "consumable_manager"
    if (candidate / "library").is_dir():
        return candidate
    return _TOOLS_DIR.parent

PACKAGE_ROOT = _locate_package()
DEFAULT_LIBRARY = PACKAGE_ROOT / "library"
DEFAULT_CONTRIBUTIONS = _TOOLS_DIR.parent / "contributions"
DRAFT_FILENAME = "user_library.json"

# 优先走正常包导入（测试环境等已有 Home Assistant mock）；
# 离线（CLI 直跑）时降级为 importlib 加载 library.py / const.py：
# 构造最小包容器 + homeassistant.const.Platform stub，使相对导入可解析。
try:  # noqa: E402
    from consumable_manager.library import (  # noqa: F401
        LibraryError,
        load_library,
        parse_consumable,
    )
except ImportError:
    import importlib.util
    import types

    _ha_pkg = types.ModuleType("homeassistant")
    _ha_pkg.__path__ = []
    sys.modules["homeassistant"] = _ha_pkg
    _ha_const = types.ModuleType("homeassistant.const")
    _ha_const.Platform = type(
        "Platform", (), {"SENSOR": "sensor", "TODO": "todo"}
    )
    sys.modules["homeassistant.const"] = _ha_const

    _pkg = types.ModuleType("consumable_manager")
    _pkg.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["consumable_manager"] = _pkg

    def _load_pkg_module(name: str, rel_path: str):
        spec = importlib.util.spec_from_file_location(
            name, PACKAGE_ROOT / rel_path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    _load_pkg_module("consumable_manager.const", "const.py")
    _load_pkg_module("consumable_manager.library", "library.py")

    from consumable_manager.library import (  # noqa: E402,F811
        LibraryError,
        load_library,
        parse_consumable,
    )

from consumable_manager.const import CONSUMABLE_UNITS  # noqa: E402

# ---- 原始数据读写 ----
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def _write_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)

def load_raw_library(root: Path) -> dict[str, Any]:
    """读内置库三个文件的原始 JSON（index / consumables / names）。"""
    return {
        "index": _read_json(root / "index.json"),
        "consumables": _read_json(root / "consumables.json"),
        "names": _read_json(root / "names.json"),
    }

# ---- 排序规范 ----
def sort_types(raw_types: dict[str, Any]) -> dict[str, Any]:
    return dict(sorted(raw_types.items()))

def sort_consumables(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 type 分组 + 组内 id 字母序。"""
    return sorted(items, key=lambda item: (item.get("type", ""), item.get("id", "")))

def sort_names(names: dict[str, Any]) -> dict[str, Any]:
    return {section: dict(sorted(section_map.items())) if isinstance(section_map, dict) else section_map
            for section, section_map in names.items()}

# ---- 锚点与内容比较 ----
def _consumable_anchor(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("type", "")).lower(), str(item.get("model", "")).strip().lower())

def _plain_name(value: Any) -> str:
    """数据文件 name 兜底：dict 取 en 或首语言；plain 原样。"""
    if isinstance(value, dict):
        for key in ("en", "zh-Hans"):
            if value.get(key):
                return value[key]
        return next(iter(value.values()), "")
    return str(value or "")

def _is_ascii(value: Any) -> bool:
    """字符串是否纯 ASCII；非字符串（数字等）按 True（不在此校验）。"""
    if not isinstance(value, str):
        return True
    try:
        value.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False

def _detect_locale(value: Any) -> str | None:
    """识别裸字符串 name 的语言，返回应填入 names.json 的 locale 键。

    含 CJK 字符 → 'zh-Hans'；纯 ASCII/拉丁 → 'en'；其他脚本
    （日/韩/西里尔/阿等）→ None（不猜测，交由人工补充）。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    has_cjk = any(
        ("\u4e00" <= ch <= "\u9fff") or ("\u3400" <= ch <= "\u4dbf")
        for ch in value
    )
    if has_cjk:
        return "zh-Hans"
    if _is_ascii(value):
        return "en"
    return None

def _content_same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """同锚点内容是否一致（name 经兜底规范化后比较）。"""
    for key in ("id", "type", "model", "unit"):
        if a.get(key) != b.get(key):
            return False
    if _plain_name(a.get("name")) != _plain_name(b.get("name")):
        return False
    return (a.get("meta") or {}) == (b.get("meta") or {})

def _type_content_same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """类型条目（锚点 = key）内容是否一致。"""
    return (
        _plain_name(a.get("name")) == _plain_name(b.get("name"))
        and a.get("icon") == b.get("icon")
        and a.get("default_threshold_type") == b.get("default_threshold_type")
        and a.get("default_threshold") == b.get("default_threshold")
        and a.get("default_threshold_unit") == b.get("default_threshold_unit")
    )

# ---- 检查（--check，CI 用）----
def check_library(root: Path) -> list[str]:
    """内置库检查：字段/引用完整性、排序规范、names 覆盖。返回问题列表。"""
    problems: list[str] = []
    try:
        load_library(root)  # 字段 / 引用完整性校验（失败抛 LibraryError）
    except LibraryError as exc:
        return [f"结构校验失败: {exc}"]

    raw = load_raw_library(root)

    # 排序规范
    index = raw["index"]
    if list(index.get("types", {})) != list(sort_types(index.get("types", {}))):
        problems.append("index.json types 未按键字母序（运行 --ingest 或人工整理）")
    if raw["consumables"] != sort_consumables(raw["consumables"]):
        problems.append("consumables.json 未按 type 分组 + id 字母序")
    names = raw["names"]
    if names != sort_names(names):
        problems.append("names.json 段内未按键字母序")

    # names 覆盖（数据文件 name 英文兜底，多语言应进 names.json；缺失仅提示）
    names_by_section = {
        "types": set(names.get("types", {})),
        "consumables": set(names.get("consumables", {})),
    }
    for key in index.get("types", {}):
        if key not in names_by_section["types"]:
            problems.append(f"提示: 类型 {key} 缺少 names.types 多语言")
    for item in raw["consumables"]:
        if item.get("id") not in names_by_section["consumables"]:
            problems.append(f"提示: 耗材 {item.get('id')} 缺少 names.consumables 多语言")
    # 未识别语言标记（und 槽）：提示人工补充正确语言并移除 und
    for key, mapping in names.get("types", {}).items():
        if isinstance(mapping, dict) and "und" in mapping:
            problems.append(
                f"提示: 类型 {key} 的 name 语言未识别（und 槽），建议人工补充正确语言并移除 und"
            )
    for cid, mapping in names.get("consumables", {}).items():
        if isinstance(mapping, dict) and "und" in mapping:
            problems.append(
                f"提示: 耗材 {cid} 的 name 语言未识别（und 槽），建议人工补充正确语言并移除 und"
            )
    return problems

# ---- 草稿验证（--validate-draft，CI 用）----
def _expected_schema(root: Path) -> int:
    """内置库当前 schema_version（草稿必须与之匹配）。"""
    try:
        idx = _read_json(root / "index.json")
        return int(idx.get("schema_version", 1))
    except Exception:
        return 1

def validate_draft(
    user_lib_path: Path, expected_schema: int, builtin_types: set[str]
) -> list[str]:
    """校验单份草稿是否符合当前合并规则。返回问题列表（非空=不合格）。"""
    problems: list[str] = []
    try:
        user = _read_json(user_lib_path)
    except Exception as exc:
        return [f"JSON 解析失败: {exc}"]
    if not isinstance(user, dict):
        return ["根应为 JSON 对象"]

    # 1. schema_version 必须与内置库当前版本一致
    sv = user.get("schema_version")
    if sv != expected_schema:
        problems.append(
            f"schema_version 应为 {expected_schema}（当前内置库版本），实际 {sv!r}"
        )

    # 2. 已废弃字段
    for forbidden in ("devices",):
        if forbidden in user:
            problems.append(f"顶层字段 '{forbidden}' 已废弃，不允许提交")

    # 3. types / consumables 结构
    types = user.get("types")
    if types is None:
        types = {}
    if not isinstance(types, dict):
        problems.append("types 应为对象")
        types = {}
    consumables = user.get("consumables")
    if consumables is None:
        consumables = []
    if not isinstance(consumables, list):
        problems.append("consumables 应为数组")
        consumables = []

    known = set(types) | set(builtin_types)
    for key, t in types.items():
        if not isinstance(t, dict):
            problems.append(f"type {key} 应为对象")
            continue
        # 代码/标识字段必须 ASCII（类型键、icon、阈值类型/单位）
        if not _is_ascii(key):
            problems.append(f"type 键 {key!r} 含非 ASCII 字符，类型键必须为英文/数字")
        name = t.get("name")
        if not name:
            problems.append(f"type {key} 缺少 name")
        elif not isinstance(name, (str, dict)):
            problems.append(f"type {key} 的 name 类型非法（应为字符串或语言对象）")
        if not t.get("icon"):
            problems.append(f"type {key} 缺少 icon")
        elif not _is_ascii(t["icon"]):
            problems.append(f"type {key} 的 icon 含非 ASCII 字符（应为 mdi:...）")
        if not _is_ascii(t.get("default_threshold_type") or ""):
            problems.append(f"type {key} 的 default_threshold_type 含非 ASCII 字符")
        if not _is_ascii(t.get("default_threshold_unit") or ""):
            problems.append(f"type {key} 的 default_threshold_unit 含非 ASCII 字符")

    for item in consumables:
        if not isinstance(item, dict):
            problems.append("consumable 应为对象")
            continue
        cid = item.get("id")
        ctype = item.get("type")
        # 代码/标识字段必须 ASCII（id、type 引用、model 型号）
        if not _is_ascii(cid or ""):
            problems.append(f"consumable id {cid!r} 含非 ASCII 字符，id 必须为英文/数字")
        if not _is_ascii(ctype or ""):
            problems.append(f"consumable type {ctype!r} 含非 ASCII 字符，type 必须为英文/数字")
        if not _is_ascii(item.get("model") or ""):
            problems.append(
                f"consumable {cid or '?'} 的 model 含非 ASCII 字符，型号须为英文/数字"
            )
        # 计量单位是 locale 无关键（存储键、显示时翻译），与 id/model 同为标识字段
        raw_unit = item.get("unit") or ""
        if not _is_ascii(raw_unit):
            problems.append(
                f"consumable {cid or '?'} 的 unit {raw_unit!r} 含非 ASCII 字符，"
                "计量单位须使用英文键（如 piece / grain / bottle）"
            )
        elif raw_unit and raw_unit not in CONSUMABLE_UNITS:
            problems.append(
                f"consumable {cid or '?'} 的 unit {raw_unit!r} 不是已知单位键，"
                f"可用值: {', '.join(CONSUMABLE_UNITS)}"
            )
        if ctype not in known:
            problems.append(f"consumable {cid} 引用未知类型 {ctype!r}")
            continue
        try:
            parse_consumable(item, user_lib_path, {k: None for k in known})
        except LibraryError as exc:
            problems.append(f"consumable {cid} 校验失败: {exc}")
    return problems

def validate_drafts(root: Path, contributions_dir: Path) -> dict[str, Any]:
    """校验 contributions/ 下全部草稿。返回 {drafts:[...], problems:{path:[...]}}。"""
    report: dict[str, Any] = {"drafts": [], "problems": {}}
    drafts = (
        sorted(p for p in contributions_dir.glob(f"*/{DRAFT_FILENAME}") if p.is_file())
        if contributions_dir.is_dir()
        else []
    )
    expected = _expected_schema(root)
    builtin_types = set(load_raw_library(root)["index"].get("types", {}))
    for draft in drafts:
        report["drafts"].append(str(draft))
        report["problems"][str(draft)] = validate_draft(draft, expected, builtin_types)
    return report

# ---- 摄入（--ingest / --dry-run）----
def ingest_user_library(
    root: Path,
    user_lib_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """摄入单份草稿（user_library.json）到内置库。返回报告（新增 / 跳过 / 需人工裁决）。"""
    report: dict[str, Any] = {"added": [], "skipped": [], "conflict": [], "warnings": []}

    # 内置库原始数据（保留 index 的 schema_version 等无关键）
    raw = load_raw_library(root)
    index = raw["index"]
    builtin_types = dict(index.get("types", {}))
    builtin_consumables = list(raw["consumables"])
    names = raw["names"]
    names_types = dict(names.get("types", {}))
    names_consumables = dict(names.get("consumables", {}))

    user = _read_json(user_lib_path)
    if not isinstance(user, dict):
        raise ValueError(f"{user_lib_path}: 应为 JSON 对象")

    # 1. types（锚点 = key）
    for key, raw_type in (user.get("types") or {}).items():
        if key in builtin_types:
            if _type_content_same(builtin_types[key], raw_type):
                report["skipped"].append(f"type {key}（同锚点同内容）")
                continue
            report["conflict"].append(f"type {key}（同锚点不同内容，需人工裁决）")
            continue
        builtin_types[key] = raw_type
        report["added"].append(f"type {key}")
        # 多语言拆解（类型显示名）：dict 直接拆；裸字符串按语言自动填 names
        name = raw_type.get("name")
        if isinstance(name, dict):
            names_types[key] = {k: v for k, v in name.items() if v}
            raw_type["name"] = _plain_name(name)
        else:
            locale = _detect_locale(name)
            if locale:
                names_types[key] = {locale: name}
            else:
                # 无法识别的语言：存入 names.json 的 'und'（undetermined）槽，
                # 既保留原文、又便于后期人工 grep 出所有待整理条目。
                names_types[key] = {"und": name}
                report["warnings"].append(
                    f"type {key} 的 name 语言无法识别（{name!r}），已存入 names.json 的 und 槽，请人工补充正确语言并移除 und"
                )

    # 2. consumables（锚点 = type + model）
    known_types = set(builtin_types) | set(user.get("types") or {})
    for raw_item in user.get("consumables") or []:
        anchor = _consumable_anchor(raw_item)
        matched = next(
            (b for b in builtin_consumables if _consumable_anchor(b) == anchor),
            None,
        )
        if matched is not None:
            if _content_same(matched, raw_item):
                report["skipped"].append(
                    f"consumable {raw_item.get('id')}（同锚点同内容）"
                )
            else:
                report["conflict"].append(
                    f"consumable {raw_item.get('id')}（同锚点不同内容，需人工裁决）"
                )
            continue
        # 新耗材：校验字段 + 引用类型
        try:
            parse_consumable(raw_item, user_lib_path, {k: None for k in known_types})
        except LibraryError as exc:
            report["conflict"].append(f"consumable {raw_item.get('id')} 校验失败: {exc}")
            continue
        if raw_item.get("type") not in known_types:
            report["conflict"].append(
                f"consumable {raw_item.get('id')} 引用未知类型 {raw_item.get('type')}"
            )
            continue
        builtin_consumables.append(raw_item)
        report["added"].append(f"consumable {raw_item.get('id')}")
        name = raw_item.get("name")
        if isinstance(name, dict):
            names_consumables[raw_item.get("id")] = {
                k: v for k, v in name.items() if v
            }
            raw_item["name"] = _plain_name(name)
        else:
            locale = _detect_locale(name)
            if locale:
                names_consumables[raw_item.get("id")] = {locale: name}
            else:
                names_consumables[raw_item.get("id")] = {"und": name}
                report["warnings"].append(
                    f"consumable {raw_item.get('id')} 的 name 语言无法识别（{name!r}），已存入 names.json 的 und 槽，请人工补充正确语言并移除 und"
                )

    # 3. 排序归位 + 写回
    index["types"] = sort_types(builtin_types)
    new_raw = {
        "index": index,
        "consumables": sort_consumables(builtin_consumables),
        "names": sort_names(
            {"types": names_types, "consumables": names_consumables}
        ),
    }
    if not dry_run:
        _write_atomic(root / "index.json", index)
        _write_atomic(root / "consumables.json", new_raw["consumables"])
        _write_atomic(root / "names.json", new_raw["names"])

    # 摄入后自检（排序 / 完整性应无问题）
    problems = check_library(root)
    report["post_check"] = problems
    return report

def ingest_contributions(
    root: Path,
    contributions_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """摄入分支 contributions/ 文件夹下的全部草稿（每份 = contributions/<用户名>/user_library.json）。"""
    report: dict[str, Any] = {
        "drafts": [], "added": [], "skipped": [], "conflict": [], "warnings": [],
    }
    drafts = (
        sorted(p for p in contributions_dir.glob(f"*/{DRAFT_FILENAME}") if p.is_file())
        if contributions_dir.is_dir()
        else []
    )
    for draft in drafts:
        report["drafts"].append(str(draft))
        sub = ingest_user_library(root, draft, dry_run=dry_run)
        report["added"].extend(sub["added"])
        report["skipped"].extend(sub["skipped"])
        report["conflict"].extend(sub["conflict"])
        report["warnings"].extend(sub.get("warnings", []))
    if drafts and not dry_run:
        problems = check_library(root)
        report["post_check"] = problems
    else:
        report["post_check"] = []
    return report

# ---- CLI ----
def main() -> int:
    parser = argparse.ArgumentParser(
        description="贡献草稿 → 内置库 摄入 / 检查工具（仅 CI 使用）"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只校验内置库（不写文件）")
    group.add_argument(
        "--ingest", action="store_true",
        help="摄入 contributions/ 文件夹下全部草稿（默认）",
    )
    group.add_argument(
        "--validate-draft", action="store_true",
        help="仅校验 contributions/ 草稿是否符合当前合并规则（不写文件）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument(
        "--contributions", type=Path, default=DEFAULT_CONTRIBUTIONS,
        help="贡献草稿目录（分支中的 contributions/ 文件夹）",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    root = args.library.resolve()

    if args.validate_draft:
        contributions = args.contributions.resolve()
        report = validate_drafts(root, contributions)
        if not report["drafts"]:
            print(f"{contributions} 下没有草稿，无需验证。")
            return 0
        bad = False
        for draft in report["drafts"]:
            probs = report["problems"][draft]
            if probs:
                bad = True
                print(f"草稿 {draft} 不符合要求：")
                for problem in probs:
                    print(f"  - {problem}")
            elif args.verbose:
                print(f"草稿 {draft} 通过校验。")
        if bad:
            print("草稿校验未通过，拒绝摄入。")
            return 1
        print("全部草稿通过校验，可以摄入。")
        return 0

    if args.check:
        problems = check_library(root)
        errors = [p for p in problems if not p.startswith("提示:")]
        warnings = [p for p in problems if p.startswith("提示:")]
        if warnings:
            print("内置库提示（不影响通过，建议补充多语言）：")
            for problem in warnings:
                print(f"  - {problem}")
        if errors:
            print("内置库检查发现问题：")
            for problem in errors:
                print(f"  - {problem}")
            return 1
        print("内置库检查通过（字段 / 引用 / 排序 / names 覆盖）。")
        return 0

    contributions = args.contributions.resolve()
    report = ingest_contributions(root, contributions, dry_run=args.dry_run)
    if not report["drafts"]:
        print(f"{contributions} 下没有草稿（*/{DRAFT_FILENAME}），无需摄入。")
        return 0

    print(
        f"摄入{'（dry-run，未写文件）' if args.dry_run else '完成'}"
        f"（{len(report['drafts'])} 份草稿）："
    )
    if args.verbose:
        for draft in report["drafts"]:
            print(f"  * {draft}")
    for label, items in (("新增", report["added"]), ("跳过", report["skipped"])):
        if items:
            print(f"  {label} {len(items)} 项")
            if args.verbose:
                for item in items:
                    print(f"    + {item}")
    if report["conflict"]:
        print(f"  需人工裁决 {len(report['conflict'])} 项（不自动覆盖）：")
        for item in report["conflict"]:
            print(f"    ! {item}")
    if report.get("warnings"):
        print(f"  语言识别提示 {len(report['warnings'])} 项：")
        for w in report["warnings"]:
            print(f"    ? {w}")
    if report["post_check"]:
        errors = [p for p in report["post_check"] if not p.startswith("提示:")]
        if errors:
            print("  摄入后自检发现问题：")
            for problem in errors:
                print(f"    - {problem}")
            return 1
        for problem in report["post_check"]:
            print(f"    · {problem}（不影响摄入，建议补充多语言）")
    return 0

if __name__ == "__main__":
    sys.exit(main())