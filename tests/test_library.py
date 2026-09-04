"""库加载 / 多语言映射 / 摄入工具 / 用户库（真环境 pytest-ha）。"""

from __future__ import annotations
import json
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parent.parent   # tests_ha 的上级 = 集成目录
REPO_ROOT = INTEGRATION.parent.parent                  # custom_components 的上级 = 仓库根

def check(desc: str, cond: bool) -> None:
    """离线 harness 的 check(desc, cond) 断言 → pytest assert。"""
    assert cond, desc

# ---- 用例 ---
def test_library_v1() -> None:
    import shutil
    import tempfile
    from consumable_manager.library import (
        LibraryError,
        load_library,
    )
    library = load_library()
    check("类型清单来自库", set(library.types) == {
        "battery", "filter", "humidifier", "ink", "robot_vacuum",
        "water_purifier"})
    meta = library.type_meta("battery")
    check("类型元数据存在", meta is not None)
    check("类型图标", meta.icon == "mdi:battery-outline")
    check("类型阈值默认三件套",
          (meta.default_threshold_type, meta.default_threshold,
           meta.default_threshold_unit) == ("lifetime_percent", 20, "%"))
    check("类型显示名(中文)", meta.display_name("zh-Hans") == "无线设备（电池）")
    check("类型显示名(英文)", meta.display_name("en") == "Battery")
    check("自定义类型无元数据", library.type_meta("自定义") is None)
    check("耗材数量（下限）", library.count >= 44)
    c = library.get("battery_cr2032")
    check("耗材字段完整",
          (c.cons_type, c.model, c.unit) == ("battery", "CR2032", "grain"))
    check("耗材显示名回退链(中文)", c.display_name("zh-Hans") == "纽扣电池 CR2032")
    check("耗材显示名回退链(英文)", c.display_name("en") == "Button cell CR2032")
    check("耗材显示名(未知 locale 回退英文)", c.display_name("de") == "Button cell CR2032")
    check("耗材 meta 保留", library.get("filter_hepa13").meta == {
        "lifetime_hours": 1500, "grade": "H13"})
    check("meta 可为空对象", library.get("robot_vacuum_mop").meta == {})
    check("文本搜索(型号)", [x.id for x in library.find_by_text("CR2032")]
          == ["battery_cr2032"])
    check("文本搜索(中文)", any(x.id == "filter_hepa13"
          for x in library.find_by_text("HEPA")))
    # ---- 校验失败场景（临时目录构造坏数据）----
    def _bad_lib(mutate) -> None:
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(INTEGRATION / "library", tmp / "library")
        mutate(tmp / "library")
        try:
            load_library(tmp / "library")
            check("坏数据应报错", False)
        except LibraryError:
            check("坏数据应报错", True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    def _drop_consumable_field(key: str):
        def _mutate(root: Path):
            path = root / "consumables.json"
            items = json.loads(path.read_text(encoding="utf-8"))
            items[0].pop(key, None)
            path.write_text(
                json.dumps(items, ensure_ascii=False, indent=2),
                encoding="utf-8")
        return _mutate
    def _unknown_type(root: Path):
        path = root / "consumables.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        items[0]["type"] = "no_such_type"
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    def _dup_id(root: Path):
        path = root / "consumables.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        items.append(dict(items[0]))
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    def _bad_schema(root: Path):
        path = root / "index.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = 99
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    _bad_lib(_drop_consumable_field("unit"))
    _bad_lib(_drop_consumable_field("meta"))
    _bad_lib(_drop_consumable_field("name"))
    _bad_lib(_unknown_type)
    _bad_lib(_dup_id)
    _bad_lib(_bad_schema)

def test_names_mapping() -> None:
    """多语言映射：names 命中优先 / 缺失语言回退数据内 name / 缺表降级。"""
    import shutil
    import tempfile
    from consumable_manager.library import (
        load_library,
        resolve_name,
    )
    check("数据内 name 为英文兜底",
          load_library().get("battery_cr2032").name == "Button cell CR2032")
    check("names 命中优先",
          resolve_name("Button cell", "zh-Hans", "x",
                       {"zh-Hans": "纽扣电池", "en": "Button cell"})
          == "纽扣电池")
    check("names 缺语言回退 en（其他语言默认英文）",
          resolve_name("Button cell", "de", "x",
                       {"zh-Hans": "纽扣电池", "en": "Button cell"})
          == "Button cell")
    check("names 无可用语言回退数据内 name",
          resolve_name("Button cell", "zh-Hans", "x", {"fr": "Pile"})
          == "Button cell")
    check("用户库 dict 兼容（无 names 表）",
          resolve_name({"zh-Hans": "国产 CR2032", "en": "CN CR2032"},
                       "zh-Hans", "x")
          == "国产 CR2032")
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(INTEGRATION / "library", tmp / "library")
        (tmp / "library" / "names.json").unlink()
        lib_no_names = load_library(tmp / "library")
        check("缺 names.json 加载成功",
              lib_no_names.type_meta("battery").display_name("zh-Hans")
              == "Battery")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    tmp2 = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(INTEGRATION / "library", tmp2 / "library")
        (tmp2 / "library" / "names.json").write_text(
            "[not json", encoding="utf-8")
        lib_bad = load_library(tmp2 / "library")
        check("names.json 损坏降级",
              lib_bad.type_meta("battery").display_name("zh-Hans")
              == "Battery")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

def test_ingest_tool() -> None:
    """摄入工具：check 校验 / 锚点去重裁决 / 多语言拆解 / 排序归位 / dry-run。"""
    import shutil
    import tempfile
    from tools import ingest as ing
    tmp = Path(tempfile.mkdtemp())
    try:
        root = tmp / "library"
        shutil.copytree(INTEGRATION / "library", root)
        check("工具 check 通过", ing.check_library(root) == [])
        user = tmp / "user_lib.json"
        user.write_text(json.dumps({
            "schema_version": 1,
            "types": {
                "vacuum_bag": {
                    "name": {"zh-Hans": "吸尘器尘袋", "en": "Vacuum bag"},
                    "icon": "mdi:air-filter",
                    "default_threshold_type": "lifetime_percent",
                    "default_threshold": 20,
                    "default_threshold_unit": "%",
                },
            },
            "consumables": [
                {
                    "id": "vacuum_bag_hepa",
                    "type": "vacuum_bag",
                    "model": "HepaBag-1",
                    "name": {"zh-Hans": "HepaBag-1 尘袋", "en": "HepaBag-1 dust bag"},
                    "unit": "个",
                    "meta": {},
                },
                {
                    "id": "battery_cr2032",
                    "type": "battery",
                    "model": "CR2032",
                    "name": "我的 CR2032",
                    "unit": "粒",
                    "meta": {},
                },
            ],
        }, ensure_ascii=False), encoding="utf-8")
        report = ing.ingest_user_library(root, user, dry_run=True)
        check("dry-run 报告新增", len(report["added"]) == 2)  # type + consumable
        check("dry-run 报告冲突", len(report["conflict"]) == 1)
        data_after = json.loads(
            (root / "consumables.json").read_text(encoding="utf-8"))
        check("dry-run 不写文件",
              not any(c.get("id") == "vacuum_bag_hepa" for c in data_after))
        report = ing.ingest_user_library(root, user, dry_run=False)
        check("摄入新增两项", len(report["added"]) == 2)
        check("摄入冲突一项", len(report["conflict"]) == 1)
        check("摄入后自检通过", report["post_check"] == [])
        cons = json.loads((root / "consumables.json").read_text(encoding="utf-8"))
        new_item = next(c for c in cons if c["id"] == "vacuum_bag_hepa")
        check("数据 name 落英文兜底", new_item["name"] == "HepaBag-1 dust bag")
        names = json.loads((root / "names.json").read_text(encoding="utf-8"))
        check("names 拆解中文",
              names["consumables"]["vacuum_bag_hepa"]["zh-Hans"]
              == "HepaBag-1 尘袋")
        check("names 类型拆解", names["types"]["vacuum_bag"]["zh-Hans"]
              == "吸尘器尘袋")
        cr2032 = next(c for c in cons if c["id"] == "battery_cr2032")
        check("冲突不覆盖内置", cr2032["name"] == "Button cell CR2032")
        check("摄入后 check 通过", ing.check_library(root) == [])
        report2 = ing.ingest_user_library(root, user, dry_run=False)
        check("再次摄入跳过", len(report2["added"]) == 0
              and len(report2["conflict"]) == 1)
        cdir = tmp / "contributions"
        (cdir / "alice").mkdir(parents=True)
        (cdir / "alice" / "user_library.json").write_text(
            json.dumps({
                "schema_version": 1,
                "consumables": [
                    {
                        "id": "battery_test_x",
                        "type": "battery",
                        "model": "TEST-X",
                        "name": {"zh-Hans": "测试电池 X", "en": "Test battery X"},
                        "unit": "节",
                        "meta": {},
                    },
                ],
            }, ensure_ascii=False), encoding="utf-8")
        rep = ing.ingest_contributions(root, cdir, dry_run=True)
        check("contributions 扫描到草稿", len(rep["drafts"]) == 1)
        check("dry-run 报告新增草稿条目", len(rep["added"]) == 1)
        cons_mid = json.loads((root / "consumables.json").read_text(encoding="utf-8"))
        check("dry-run 不写文件",
              not any(c.get("id") == "battery_test_x" for c in cons_mid))
        rep = ing.ingest_contributions(root, cdir, dry_run=False)
        check("批量摄入新增", len(rep["added"]) == 1)
        check("批量摄入自检通过", rep["post_check"] == [])
        empty = ing.ingest_contributions(root, tmp / "no_such_dir")
        check("缺失目录空操作", empty["drafts"] == [] and empty["post_check"] == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---- 用户库（单文件覆盖层：同锚点用户优先、整条替换、坏文件降级）----
async def test_user_library(hass) -> None:
    import tempfile
    from consumable_manager.library import load_library
    from consumable_manager.user_library import (
        async_load_library,
        load_merged_library,
    )
    builtin = load_library()
    def _with_user(data) -> object:
        """把用户库数据写入临时配置目录并做合并加载。"""
        tmp = Path(tempfile.mkdtemp(prefix="cm_test_user_"))
        path = tmp / "user_library.json"
        path.write_text(
            data if isinstance(data, str)
            else json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return load_merged_library(None, path)
    # ---- 文件缺失 / 空文件 → 等价内置 ----
    missing = load_merged_library(
        None, Path(tempfile.mkdtemp()) / "user_library.json"
    )
    check("用户库缺失回退内置",
          set(missing.types) == set(builtin.types)
          and missing.count == builtin.count)
    empty = _with_user({})
    check("空对象等价内置", empty.count == builtin.count)
    nulls = _with_user({"types": None, "consumables": None})
    check("空段等价内置", nulls.count == builtin.count)
    # ---- 正常覆盖 + 新增 ----
    user_data = {
        "schema_version": 1,
        "types": {
            "battery": {
                "name": {"zh-Hans": "我的电池类", "en": "My Battery"},
                "icon": "mdi:battery-10",
                "default_threshold_type": "lifetime_percent",
                "default_threshold": 30,
                "default_threshold_unit": "%",
            },
            "toothbrush_head": {
                "name": "牙刷替换头",
                "icon": "mdi:toothbrush",
                "default_threshold_type": "used_time",
                "default_threshold": 90,
                "default_threshold_unit": "days",
            },
        },
        "consumables": [
            {
                "id": "battery_cr2032", "type": "battery",
                "model": "CR2032", "name": "国产 CR2032",
                "unit": "颗", "meta": {"voltage": 3},
            },
            {
                "id": "toothbrush_head_s", "type": "toothbrush_head",
                "model": "S", "name": "牙刷头 S", "unit": "个", "meta": {},
            },
            {
                "id": "filter_custom", "type": "filter",
                "model": "F-99", "name": "自制滤芯", "unit": "片",
                "meta": {},
            },
        ],
    }
    m = _with_user(user_data)
    check("覆盖类型图标", m.type_meta("battery").icon == "mdi:battery-10")
    check("覆盖类型阈值默认", m.type_meta("battery").default_threshold == 30)
    check("覆盖类型显示名",
          m.type_meta("battery").display_name("zh-Hans") == "我的电池类")
    check("新增类型", "toothbrush_head" in m.types)
    check("类型总数 6+1", len(m.types) == 7)
    check("覆盖耗材整条替换",
          m.get("battery_cr2032").display_name("zh-Hans") == "国产 CR2032"
          and m.get("battery_cr2032").unit == "颗"
          and m.get("battery_cr2032").meta == {"voltage": 3})
    check("耗材总数（下限 44+2）", m.count >= 46)
    check("自定义类型耗材可查", m.get("toothbrush_head_s") is not None)
    check("内置类型新增耗材",
          any(c.id == "filter_custom" for c in m.by_type("filter")))
    # ---- 坏文件整体降级（不抛错、回退内置）----
    def _degraded(name: str, data) -> None:
        try:
            lib = _with_user(data)
            check(name, set(lib.types) == set(builtin.types)
                  and lib.count == builtin.count)
        except Exception:  # noqa: BLE001 - 降级语义即不允许抛错
            check(name, False)
    _degraded("坏 JSON 降级", "{ not json")
    _degraded("未知类型降级", {"consumables": [{
        "id": "x_1", "type": "no_such_type", "model": "M",
        "name": "x", "unit": "个", "meta": {},
    }]})
    _degraded("耗材缺字段降级", {"consumables": [{
        "id": "x_1", "type": "filter", "model": "M", "name": "x",
    }]})
    _degraded("耗材 id 重复降级", {"consumables": [
        {"id": "x_1", "type": "filter", "model": "M", "name": "x",
         "unit": "个", "meta": {}},
        {"id": "x_1", "type": "filter", "model": "M2", "name": "y",
         "unit": "个", "meta": {}},
    ]})
    _degraded("schema 版本不符降级", {
        "schema_version": 99,
        "consumables": [{"id": "x_1", "type": "filter", "model": "M",
                         "name": "x", "unit": "个", "meta": {}}],
    })
    # ---- 异步入口（全集成统一的合并加载）----
    cfg = Path(tempfile.mkdtemp(prefix="cm_test_async_"))
    (cfg / ".consumable_manager").mkdir()
    (cfg / ".consumable_manager" / "user_library.json").write_text(
        json.dumps(user_data, ensure_ascii=False), encoding="utf-8")
    hass.config.config_dir = str(cfg)  # 真实 hass：指向含用户库的临时配置目录
    merged_async = await async_load_library(hass)
    check("异步合并加载", merged_async.count == builtin.count + 2
          and "toothbrush_head" in merged_async.types)
    hass.config.config_dir = tempfile.mkdtemp(prefix="cm_test_async_")
    merged_async2 = await async_load_library(hass)
    check("异步无用户库回退内置", merged_async2.count == builtin.count)
    # ---- 写库函数：必填字段对齐内置库（缺任一必填即抛错，绝不写非法条目）----
    from consumable_manager.library import LibraryError
    from consumable_manager.user_library import (
        read_user_library,
        write_user_consumable,
    )
    wtmp = Path(tempfile.mkdtemp(prefix="cm_test_write_"))
    wpath = wtmp / "user_library.json"
    for missing, args in (
        ("型号", ("filter", "", "滤芯", "个")),
        ("名称", ("filter", "F1", "   ", "个")),
        ("单位", ("filter", "F1", "滤芯", "")),
    ):
        try:
            write_user_consumable(wpath, *args)
            check(f"缺{missing}拒绝", False)
        except LibraryError:
            check(f"缺{missing}拒绝", True)
    cid1 = write_user_consumable(wpath, "filter", "MyF1", "自制滤芯", "片")
    check("id 按类型+型号生成", cid1 == "filter_myf1")
    user1 = read_user_library(wpath, builtin)
    check("必填单条写入", len(user1.consumables) == 1)
    w1 = user1.consumables[0]
    check("name 纯字符串写入", w1.name == "自制滤芯")
    check("model 原样保留", w1.model == "MyF1")
    check("unit 原样保留", w1.unit == "片")
    check("meta 缺省空对象", w1.meta == {})
    check("schema 版本被修正", json.loads(
        wpath.read_text(encoding="utf-8"))["schema_version"] == 1)
    cid1b = write_user_consumable(wpath, "filter", "MyF1", "自制滤芯", "片")
    check("同输入 id 稳定", cid1b == "filter_myf1")
    user1b = read_user_library(wpath, builtin)
    check("幂等不重复", len(user1b.consumables) == 1)
    linked = write_user_consumable(wpath, "battery", "AA", "5号电池", "节")
    check("同型号撞内置链接", linked == "battery_aa")
    user1c = read_user_library(wpath, builtin)
    check("链接不写用户库", len(user1c.consumables) == 1)
    cid_c = write_user_consumable(wpath, "battery", "AA-", "国产AA电池", "节")
    check("异型号撞内置追加哈希",
          cid_c.startswith("battery_aa_") and cid_c != "battery_aa")
    user1d = read_user_library(wpath, builtin)
    check("哈希条目已写入", len(user1d.consumables) == 2)
    cid_u1 = write_user_consumable(wpath, "filter", "F1", "滤芯A", "个")
    check("用户库首写稳定", cid_u1 == "filter_f1")
    cid_u2 = write_user_consumable(wpath, "filter", "F1", "滤芯B", "个")
    check("用户库同型号幂等覆盖", cid_u2 == "filter_f1")
    user1e = read_user_library(wpath, builtin)
    check("幂等覆盖不新增", len(user1e.consumables) == 3)
    overwritten = next(
        c for c in user1e.consumables if c.id == "filter_f1"
    )
    check("覆盖后内容为最新", overwritten.name == "滤芯B"
          and overwritten.unit == "个")
    try:
        write_user_consumable(wpath, "no_such_type", "M", "x", "个")
        check("未知类型拒绝", False)
    except LibraryError:
        check("未知类型拒绝", True)
    try:
        write_user_consumable(wpath, "filter", "滤芯M", "x", "个")
        check("非 ASCII model 拒绝", False)
    except LibraryError:
        check("非 ASCII model 拒绝", True)
