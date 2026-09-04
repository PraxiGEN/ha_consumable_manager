"""服务（添加 / 绑定 / 查询 / 调整）（真环境 pytest-ha）。"""

from __future__ import annotations
import json
from pathlib import Path
from homeassistant.exceptions import ServiceValidationError
from _helpers import (
    INTEGRATION,
    LABELS,
    check,
    clean_persist,
    inject_entries,
    install_register_spy,
    make_call,
    make_entry,

)

from consumable_manager.coordinator import (
    ConsumableManagerData,
    ConsumableTypeCoordinator,
    StockCoordinator,

)

from consumable_manager.const import (
    CONF_CONSUMABLE_ID,
    CONF_ENTRY_TYPE,
    CONF_ITEM_ID,
    CONF_ITEM_NAME,
    CONF_ITEM_TYPE,
    CONF_QUANTITY,
    CONF_SOURCE_ENTITIES,
    CONF_BINDING_GROUPS,
    CONF_GROUP_ID,
    CONF_GROUP_NAME,
    CONF_STOCK_ITEMS,
    CONF_STOCK_THRESHOLD,
    CONF_UNIT,
    ENTRY_TYPE_STOCK,

)

from consumable_manager import services as svc

_INTEGRATION_DIR = INTEGRATION  # 集成目录（_helpers 按 manifest.json 自适应定位）

# ---- 用例 ----
async def test_services(hass, monkeypatch) -> None:
    clean_persist(hass)
    # ---- 注册：spy 记录（真 ServiceRegistry 用 __slots__，实例方法不可
    registered, handlers = install_register_spy(monkeypatch, hass)
    await svc.async_setup_services(hass)
    check("注册 7 个服务", set(registered) == {
        "bind_entity", "unbind_entity", "query_binding", "add_consumable",
        "add_type", "query_data", "adjust_stock"})
    resp_meta = registered["query_data"]
    check("query_data 仅返回数据",
          getattr(resp_meta, "value", resp_meta) == "only")
    res_single = await handlers["query_data"](
        make_call(hass, "query_data", {"data_type": "types"})
    )
    check("注册处理器单参可调用（HA 调用方式）",
          isinstance(res_single, dict) and "types" in res_single)
    # ---- 条目与协调器 ----
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [{
            CONF_ITEM_ID: "it1", CONF_ITEM_NAME: "滤芯",
            CONF_ITEM_TYPE: "filter", CONF_UNIT: "个",
            CONF_QUANTITY: 5, CONF_STOCK_THRESHOLD: 1,
        }]},
    )
    stock_coord = StockCoordinator(hass, stock_entry, LABELS)
    stock_entry.runtime_data = ConsumableManagerData(coordinator=stock_coord)
    filter_entry = make_entry("flt1", "滤芯", {CONF_ENTRY_TYPE: "filter"}, {
        CONF_BINDING_GROUPS: [{
            CONF_GROUP_ID: "default",
            CONF_GROUP_NAME: "滤芯",
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.filter_life"}],
        }],
    })
    filter_coord = ConsumableTypeCoordinator(hass, filter_entry, LABELS)
    filter_entry.runtime_data = ConsumableManagerData(coordinator=filter_coord)
    battery_entry = make_entry("bat1", "电池", {CONF_ENTRY_TYPE: "battery"}, {})
    battery_coord = ConsumableTypeCoordinator(hass, battery_entry, LABELS)
    battery_entry.runtime_data = ConsumableManagerData(coordinator=battery_coord)
    inject_entries(monkeypatch, hass, [stock_entry, filter_entry, battery_entry])
    # ---- 库存调整（add/consume 合并，action 可选）----
    res = await svc.async_adjust_stock(
        hass, make_call(hass, "adjust_stock",
                        {"action": "add", "item": "it1", "quantity": 3}))
    check("增加库存", res["quantity"] == 8)
    res2 = await svc.async_adjust_stock(
        hass, make_call(hass, "adjust_stock",
                        {"action": "consume", "item": "it1", "quantity": 3}))
    check("减少库存", res2["quantity"] == 5)
    # ---- 绑定：手动（并关联库存项）----
    res4 = await svc.async_bind_entity(hass, make_call(hass, "bind_entity", {
        "entity_id": "sensor.filter_life",
        "consumable_id": "filter_hepa13",
        "item": "it1",
    }))
    check("手动绑定返回", res4["entry_type"] == "filter"
          and res4["matched_by"] == "manual")
    check("绑定写入独立层",
          svc.bindings.get_binding(hass, "sensor.filter_life") == "filter_hepa13")
    linked = stock_entry.options[CONF_STOCK_ITEMS][0]
    check("库存项关联耗材",
          linked.get(CONF_CONSUMABLE_ID) == "filter_hepa13"
          and linked.get(CONF_ITEM_TYPE) == "filter")
    # ---- services.yaml 驱动前端字段（选项走 translation_key 翻译）----
    yaml_text = (_INTEGRATION_DIR / "services.yaml").read_text(encoding="utf-8")
    check("yaml 定义服务字段",
          "entity_id:" in yaml_text and "cons_type:" in yaml_text
          and "data_type:" in yaml_text and "quantity:" in yaml_text)
    check("yaml 选项走翻译键",
          "translation_key: service_action" in yaml_text
          and "translation_key: service_data_type" in yaml_text)
    check("库存项为实体选择器",
          "integration: consumable_manager" in yaml_text)
    check("cons_type 为 select 下拉",
          "cons_type:" in yaml_text and "select:" in yaml_text
          and "custom_value: true" in yaml_text
          and "\n            - filter\n" in yaml_text
          and "\n            - battery\n" in yaml_text)
    # ---- 库存项字段支持实体反查（实体选择器值 → item_id）----
    hass.states.async_set(
        "sensor.stock_it1", "3", {CONF_ITEM_ID: "it1"})
    res_entity = await svc.async_adjust_stock(hass, make_call(
        hass, "adjust_stock",
        {"action": "add", "item": "sensor.stock_it1", "quantity": 1}))
    check("实体反查库存项", res_entity["item_id"] == "it1")
    # ---- 绑定：未手输耗材 + 选关联库存项 → 继承库存项 consumable_id ----
    res_stock = await svc.async_bind_entity(hass, make_call(hass, "bind_entity", {
        "entity_id": "sensor.filter_life",
        "item": "it1",
    }))
    check("继承库存项耗材",
          res_stock["matched_by"] == "stock"
          and res_stock["consumable_id"] == "filter_hepa13")
    # ---- 查询绑定（按实体 / 按库存项）----
    res6 = await svc.async_query_binding(hass, make_call(
        hass, "query_binding", {"entity_id": "sensor.filter_life"}))
    b6 = next(b for b in res6["bindings"]
              if b["entity_id"] == "sensor.filter_life")
    check("查询绑定含耗材型号",
          b6["entity_id"] == "sensor.filter_life"
          and b6["consumable_id"] == "filter_hepa13"
          and b6["consumable_model"] is not None
          and b6["consumable_name"] is not None)
    res6b = await svc.async_query_binding(hass, make_call(
        hass, "query_binding", {"item": "it1"}))
    check("按库存项查询",
          any(b["consumable_id"] == "filter_hepa13" for b in res6b["bindings"]))
    # ---- 解绑（清除实体→耗材映射，实体仍留分组监控）----
    res_ub = await svc.async_unbind_entity(hass, make_call(
        hass, "unbind_entity", {"entity_id": "sensor.filter_life"}))
    check("解绑返回",
          res_ub["unbound_count"] == 1
          and res_ub["unbound_from"][0]["consumable_id"] == "filter_hepa13")
    res_after = await svc.async_query_binding(hass, make_call(
        hass, "query_binding", {"entity_id": "sensor.filter_life"}))
    ub_match = [b for b in res_after["bindings"]
                if b["entity_id"] == "sensor.filter_life"]
    check("解绑后绑定层已删除该实体映射",
          len(ub_match) == 0)
    try:
        await svc.async_unbind_entity(hass, make_call(
            hass, "unbind_entity", {"entity_id": "sensor.filter_life"}))
        check("重复解绑报错", False)
    except ServiceValidationError:
        check("重复解绑报错", True)
    try:
        await svc.async_unbind_entity(hass, make_call(
            hass, "unbind_entity", {"entity_id": "sensor.nope"}))
        check("解绑未知实体报错", False)
    except ServiceValidationError:
        check("解绑未知实体报错", True)
    # ---- 添加耗材（写入用户库，ID 自动生成，schema v1 必填口径）----
    ulib_path = (
        Path(hass.config.config_dir) / ".consumable_manager" / "user_library.json"
    )
    def _ulib() -> dict:
        if not ulib_path.is_file():
            return {}
        return json.loads(ulib_path.read_text(encoding="utf-8"))
    res7 = await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
        "cons_type": "filter", "model": "T-1", "name": "测试滤芯",
        "unit": "piece", "meta": {"grade": "T1"},
    }))
    check("添加耗材 id 自动生成", res7["consumable_id"] == "filter_t_1")
    check("添加耗材返回内容", res7["added"]["name"] == "测试滤芯"
          and res7["added"]["type"] == "filter"
          and res7["added"]["meta"] == {"grade": "T1"})
    check("添加耗材返回用户库路径",
          res7["path"].endswith("user_library.json"))
    u = _ulib()
    check("用户库含新耗材", any(
        c["id"] == "filter_t_1" and c["model"] == "T-1"
        for c in u.get("consumables", [])))
    res7b = await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
        "cons_type": "filter", "model": "T-1", "name": "测试滤芯2",
        "unit": "piece", "meta": {},
    }))
    check("幂等 id 稳定", res7b["consumable_id"] == "filter_t_1")
    u = _ulib()
    check("幂等不重复写入", sum(
        1 for c in u.get("consumables", [])
        if c["id"] == "filter_t_1") == 1)
    res_link = await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
        "cons_type": "battery", "model": "AA", "name": "5号电池", "unit": "cell",
    }))
    check("撞内置链接", res_link["consumable_id"] == "battery_aa")
    u = _ulib()
    check("链接不写用户库",
          all(c["id"] != "battery_aa" for c in u.get("consumables", [])))
    try:
        await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
            "cons_type": "filter", "model": "T-2", "name": "x",
        }))
        check("缺 unit 报错", False)
    except ServiceValidationError:
        check("缺 unit 报错", True)
    try:
        await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
            "cons_type": "filter", "model": "T-2b", "name": "x", "unit": "个",
        }))
        check("非法 unit 报错", False)
    except ServiceValidationError:
        check("非法 unit 报错", True)
    try:
        await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
            "cons_type": "filter", "model": "T-3", "name": "x", "unit": "piece",
            "meta": "oops",
        }))
        check("meta 非对象报错", False)
    except ServiceValidationError:
        check("meta 非对象报错", True)
    try:
        await svc.async_add_consumable(hass, make_call(hass, "add_consumable", {
            "cons_type": "no_such_type", "model": "T-4", "name": "x",
            "unit": "piece",
        }))
        check("未知类型报错", False)
    except ServiceValidationError:
        check("未知类型报错", True)
    # ---- 添加自定义类型（写入用户库 types 段，新建语义）----
    res_type = await svc.async_add_type(hass, make_call(hass, "add_type", {
        "type_key": "air_con", "name": "空调滤网",
        "default_threshold_type": "lifetime_percent",
        "default_threshold": 25, "default_threshold_unit": "%",
    }))
    check("添加类型返回", res_type["type_key"] == "air_con"
          and res_type["added"]["icon"] == "mdi:package-variant"
          and res_type["added"]["default_threshold"] == 25.0)
    u = _ulib()
    check("用户库含自定义类型",
          u.get("types", {}).get("air_con", {}).get("name") == "空调滤网")
    await svc.async_add_type(hass, make_call(hass, "add_type", {
        "type_key": "AirCon2", "name": "空调滤网 2",
        "default_threshold_type": "used_time",
        "default_threshold": 720, "default_threshold_unit": "hours",
    }))
    u = _ulib()
    check("类型键小写归一", "aircon2" in u.get("types", {}))
    try:
        await svc.async_add_type(hass, make_call(hass, "add_type", {
            "type_key": "filter", "name": "x",
            "default_threshold_type": "lifetime_percent",
            "default_threshold": 20, "default_threshold_unit": "%",
        }))
        check("重复类型键报错", False)
    except ServiceValidationError:
        check("重复类型键报错", True)
    try:
        await svc.async_add_type(hass, make_call(hass, "add_type", {
            "type_key": "Bad Key!", "name": "x",
            "default_threshold_type": "lifetime_percent",
            "default_threshold": 20, "default_threshold_unit": "%",
        }))
        check("非法类型键报错", False)
    except ServiceValidationError:
        check("非法类型键报错", True)
    for label, data in (
        ("缺名称", {"type_key": "t1", "name": "",
                    "default_threshold_type": "lifetime_percent",
                    "default_threshold": 20,
                    "default_threshold_unit": "%"}),
        ("非法阈值类型", {"type_key": "t2", "name": "x",
                         "default_threshold_type": "bogus",
                         "default_threshold": 20,
                         "default_threshold_unit": "%"}),
        ("负阈值", {"type_key": "t3", "name": "x",
                    "default_threshold_type": "lifetime_percent",
                    "default_threshold": -1,
                    "default_threshold_unit": "%"}),
        ("非法阈值单位", {"type_key": "t4", "name": "x",
                         "default_threshold_type": "lifetime_percent",
                         "default_threshold": 20,
                         "default_threshold_unit": "bogus"}),
    ):
        try:
            await svc.async_add_type(hass, make_call(hass, "add_type", data))
            check(f"{label}报错", False)
        except ServiceValidationError:
            check(f"{label}报错", True)
    lib2 = await svc.async_load_library(hass)
    check("自定义类型进合并库",
          "air_con" in lib2.types and "aircon2" in lib2.types)
    # ---- 查询数据（必须指定 data_type）----
    try:
        await svc.async_query_data(hass, make_call(hass, "query_data", {}))
        check("缺 data_type 报错", False)
    except ServiceValidationError:
        check("缺 data_type 报错", True)
    try:
        await svc.async_query_data(hass, make_call(
            hass, "query_data", {"data_type": "nope"}))
        check("未知 data_type 报错", False)
    except ServiceValidationError:
        check("未知 data_type 报错", True)
    res_stock = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "stock"}))
    check("查询库存条目", res_stock["stock"][0]["items"][0]["quantity"] == 6)
    res_te = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "type_entry"}))
    check("查询类型条目", len(res_te["type_entries"]) == 2)
    check("类型条目含触发字段",
          "triggered_entities" in res_te["type_entries"][0])
    res_te_f = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "type_entry", "entry_id": "bat1"}))
    check("type_entry 忽略 entry_id 直接返回全量",
          len(res_te_f["type_entries"]) == 2)
    res_t = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "types"}))
    check("查询类型元数据",
          isinstance(res_t["types"], list) and len(res_t["types"]) >= 1)
    res_c = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "consumables"}))
    check("查询耗材", isinstance(res_c["consumables"], list))
    res_c_f = await svc.async_query_data(hass, make_call(
        hass, "query_data",
        {"data_type": "consumables", "consumable_type": "filter"}))
    check("耗材按类型过滤",
          all(c["type"] == "filter" for c in res_c_f["consumables"]))
    library = await svc.async_load_library(hass)
    filter_coord.update_library(library)
    res_gd = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "group_data"}))
    check("查询分组实体数据", isinstance(res_gd["group_data"], list))
    await svc.async_bind_entity(hass, make_call(hass, "bind_entity", {
        "entity_id": "sensor.filter_life",
        "consumable_id": "filter_hepa13",
    }))
    res_gd2 = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "group_data"}))
    gd_entity: dict | None = None
    for g in res_gd2["group_data"]:
        for e in g["normal_entities"] + g["triggered_entities"]:
            gd_entity = e
            break
        if gd_entity is not None:
            break
    check("分组实体数据成员含 consumable 字段",
          gd_entity is not None and "consumable" in gd_entity)
    check("已绑定耗材名称回填",
          gd_entity is not None and gd_entity["consumable"] is not None)
    res_gd_ct = await svc.async_query_data(hass, make_call(
        hass, "query_data",
        {"data_type": "group_data", "consumable_type": "filter"}))
    check("group_data 按耗材类型过滤",
          all(g["entry_type"] == "filter" for g in res_gd_ct["group_data"]))
    res_gd_g = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "group_data", "group_entity": "default"}))
    check("group_data 按分组过滤",
          len(res_gd_g["group_data"]) == 1
          and res_gd_g["group_data"][0]["group_id"] == "default")
    from homeassistant.helpers import entity_registry as _er_mod
    class _RegEntry:
        def __init__(self, uid):
            self.unique_id = uid
    class _FakeReg:
        def async_get(self, entity_id):
            return _RegEntry("flt1_grp_default")
    monkeypatch.setattr(_er_mod, "async_get", lambda hass: _FakeReg())
    res_gd_reg = await svc.async_query_data(hass, make_call(
        hass, "query_data",
        {"data_type": "group_data", "group_entity": "sensor.ke_ting_grp"}))
    check("group_entity 反查解析为 group_id",
          len(res_gd_reg["group_data"]) == 1
          and res_gd_reg["group_data"][0]["group_id"] == "default")
    # ---- 防御：真实 HA 加载中条目尚无 runtime_data 属性，不应报错 ----
    pending = make_entry("pend1", "加载中", {CONF_ENTRY_TYPE: "filter"}, {})
    del pending.runtime_data
    inject_entries(monkeypatch, hass, [stock_entry, filter_entry, battery_entry, pending])
    res_pend = await svc.async_query_data(hass, make_call(
        hass, "query_data", {"data_type": "stock"}))
    check("未配置条目防御", isinstance(res_pend["stock"], list))

# ---- 绑定不污染分组（每类型一次）----
async def test_services_group_targeting(hass, monkeypatch) -> None:
    """绑定只写独立映射层，不把实体加进任何类型条目的分组（纯映射）。"""
    clean_persist(hass)
    battery_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {CONF_GROUP_ID: "living", CONF_GROUP_NAME: "客厅",
                 CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.bat_living"}]},
                {CONF_GROUP_ID: "bedroom", CONF_GROUP_NAME: "卧室",
                 CONF_SOURCE_ENTITIES: []},
            ],
        },
    )
    battery_coord = ConsumableTypeCoordinator(hass, battery_entry, LABELS)
    battery_entry.runtime_data = ConsumableManagerData(
        coordinator=battery_coord)
    inject_entries(monkeypatch, hass, [battery_entry])
    await svc.async_bind_entity(hass, make_call(hass, "bind_entity", {
        "entity_id": "sensor.bat_new",
        "consumable_id": "battery_cr2032",
    }))
    check("绑定写入独立层",
          svc.bindings.get_binding(hass, "sensor.bat_new") == "battery_cr2032")
    groups = battery_entry.options[CONF_BINDING_GROUPS]
    by_id = {g[CONF_GROUP_ID]: g for g in groups}
    check("分组未被绑定污染",
          not any(s.get("entity_id") == "sensor.bat_new"
                  for g in by_id.values()
                  for s in g.get(CONF_SOURCE_ENTITIES, [])))
    check("living 原有实体仍在",
          any(s.get("entity_id") == "sensor.bat_living"
              for s in by_id["living"][CONF_SOURCE_ENTITIES]))
