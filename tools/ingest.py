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
    return problems


# ---- 摄入（--ingest / --dry-run）----

def ingest_user_library(
    root: Path,
    user_lib_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """摄入单份草稿（user_library.json）到内置库。返回报告（新增 / 跳过 / 需人工裁决）。"""
    report: dict[str, Any] = {"added": [], "skipped": [], "conflict": []}

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
        # 多语言拆解（类型显示名）
        if isinstance(raw_type.get("name"), dict):
            names_types[key] = {k: v for k, v in raw_type["name"].items() if v}
            raw_type["name"] = _plain_name(raw_type["name"])

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
        if isinstance(raw_item.get("name"), dict):
            names_consumables[raw_item.get("id")] = {
                k: v for k, v in raw_item["name"].items() if v
            }
            raw_item["name"] = _plain_name(raw_item["name"])

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
    report["post_check"] = [p for p in problems if not p.startswith("提示:")]
    return report


def ingest_contributions(
    root: Path,
    contributions_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """摄入分支 contributions/ 文件夹下的全部草稿（每份 = contributions/<用户名>/user_library.json）。

    只读取该文件夹，不涉及任何本地路径；文件夹缺失或无草稿时为空操作。
    """
    report: dict[str, Any] = {
        "drafts": [], "added": [], "skipped": [], "conflict": [],
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
    if drafts and not dry_run:
        problems = check_library(root)
        report["post_check"] = [p for p in problems if not p.startswith("提示:")]
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
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument(
        "--contributions", type=Path, default=DEFAULT_CONTRIBUTIONS,
        help="贡献草稿目录（分支中的 contributions/ 文件夹）",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    root = args.library.resolve()

    if args.check:
        problems = check_library(root)
        if problems:
            print("内置库检查发现问题：")
            for problem in problems:
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
    if report["post_check"]:
        print("  摄入后自检发现问题：")
        for problem in report["post_check"]:
            print(f"    - {problem}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
