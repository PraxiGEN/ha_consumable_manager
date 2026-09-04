"""配置流（添加 / 选项 / 自定义实体表单 / 条目置顶）（真环境 pytest-ha）。"""

from __future__ import annotations
import datetime
import json
from pathlib import Path
from uuid import uuid4
import voluptuous as vol
from homeassistant.helpers import selector as _sel
from _helpers import (
    LABELS,
    check,
    clean_persist,
    inject_entries,
    make_entry,

)

from consumable_manager.coordinator import (  # noqa: E402
    ConsumableTypeCoordinator,
    StockCoordinator,

)

from consumable_manager.const import (  # noqa: E402
    DOMAIN,
    ADD_METHOD_CONSUMABLE,
    ADD_METHOD_CUSTOM_CONSUMABLE,
    CONF_ADD_METHOD,
    CONF_CONSUMABLE_ID,
    CONF_ENTITY_REGEX,
    CONF_ENTRY_TYPE,
    CONF_ITEM_ID,
    CONF_ITEM_NAME,
    CONF_ITEM_TYPE,
    CONF_MODEL,
    CONF_NOTIFICATION,
    CONF_NOTIFY_CUSTOMIZE,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_MODE,
    CONF_NOTIFY_SCHEDULE_TIME,
    CONF_NOTIFY_STYLE,
    CONF_NOTIFY_SYSTEM,
    NOTIFY_MODE_REALTIME,
    NOTIFY_MODE_SCHEDULED,
    NOTIFY_STYLE_HUMAN,
    NOTIFY_STYLE_VALUE,
    CONF_QUANTITY,
    CONF_SOURCE_ENTITIES,
    CONF_BINDING_GROUPS,
    CONF_ADDED_AT,
    CONF_LIFESPAN,
    CONF_LIFESPAN_UNIT,
    CONF_GROUP_ID,
    CONF_GROUP_KIND,
    CONF_GROUP_NAME,
    GROUP_KIND_BINDING,
    GROUP_KIND_CUSTOM,
    custom_consumable_entity_id,
    CONF_SELECTED_GROUP,
    CONF_REMOVE_GROUPS,
    CONF_STOCK_ITEMS,
    CONF_STOCK_THRESHOLD,
    CONF_THRESHOLD,
    CONF_THRESHOLD_OPERATOR,
    CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT,
    CONF_UNIT,
    CONF_TYPE_ICON,
    CONF_TYPE_KEY,
    CONF_TYPE_NAME_ZH,
    CONF_TYPE_THRESHOLD,
    CONF_TYPE_THRESHOLD_TYPE,
    CONF_TYPE_THRESHOLD_UNIT,
    ENTRY_SORT_PREFIXES,
    ENTRY_TYPE_CUSTOM,
    ENTRY_TYPE_NOTIFICATION,
    ENTRY_TYPE_STOCK,
    OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_REMAINING_TIME,
    UNIT_DAYS,

)

from consumable_manager.config_flow import (  # noqa: E402
    ConsumableManagerConfigFlow,
    ConsumableManagerOptionsFlow,
    _build_entry_type_options,
    async_entry_type_title,

)

from consumable_manager import bindings  # noqa: E402
import consumable_manager.config_flow as _cf  # noqa: E402

async def _no_translations(hass) -> dict:
    """空翻译：断言语义不依赖翻译内容（标题回退库元数据 / 原始串）。"""
    return {}

def _patch_translations(monkeypatch) -> None:
    """把 config_flow 模块的翻译读取替成空（不经真实 loader）。"""
    monkeypatch.setattr(_cf, "_get_translations", _no_translations)

def _zh(hass) -> None:
    """库元数据 / 耗材显示名按 zh-Hans 解析。"""
    hass.config.language = "zh-Hans"

def _new_flow(flow_cls, hass, handler: str):
    """装配一个可用 flow：hass / handler / flow_id / 可变 context。"""
    flow = flow_cls()
    flow.hass = hass
    flow.handler = handler
    flow.flow_id = uuid4().hex
    flow.context = {}  # 类属性是只读 MappingProxyType，async_set_unique_id 需写入
    return flow

def _schema_dict(schema) -> dict:
    """async_show_form 返回的 data_schema → {marker: validator} dict。"""
    if schema is None:
        return {}
    raw = getattr(schema, "schema", schema)
    return raw if isinstance(raw, dict) else {}

def _schema_keys(schema) -> set:
    """表单字段键集合（真实 marker 键在 .schema，离线桩在 .key）。"""
    return {getattr(m, "schema", m) for m in _schema_dict(schema)}

def _schema_field(schema, key: str) -> tuple:
    """取表单某字段的 (marker, validator)；缺省 (None, None)。"""
    for m, v in _schema_dict(schema).items():
        if getattr(m, "schema", None) == key:
            return m, v
    return None, None

def _field_default(schema, key: str) -> tuple[bool, object]:
    """取字段是否设了 default 与取值。"""
    m, _v = _schema_field(schema, key)
    if m is None:
        return False, None
    d = getattr(m, "default", vol.UNDEFINED)
    if d is vol.UNDEFINED:
        return False, None
    return True, (d() if callable(d) else d)

def _user_lib(hass) -> dict:
    """读当前（共享 testing_config）用户库文件。"""
    p = Path(hass.config.config_dir) / ".consumable_manager" / "user_library.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))

# ---- 用例 ----
async def test_config_flow_add(hass, monkeypatch) -> None:
    _zh(hass)
    clean_persist(hass)
    _patch_translations(monkeypatch)
    flow = _new_flow(ConsumableManagerConfigFlow, hass, DOMAIN)
    res = await flow.async_step_user()
    check("添加界面为表单", res["type"] == "form")
    res2 = await flow.async_step_user({CONF_ENTRY_TYPE: "battery"})
    check("创建条目", res2["type"] == "create_entry")
    check("unique_id 设为类型", flow.unique_id == "battery")
    check("数据含 entry_type", res2["data"][CONF_ENTRY_TYPE] == "battery")
    check("标题来自库元数据", res2["title"] == "无线设备（电池）")
    res_c = await flow.async_step_user({CONF_ENTRY_TYPE: ENTRY_TYPE_CUSTOM})
    check("custom 进入二级向导",
          res_c["type"] == "form" and res_c["step_id"] == "custom_type")
    icon_sel = any(
        isinstance(v, _sel.IconSelector)
        for v in _schema_dict(res_c["data_schema"]).values()
    )
    check("图标字段接入 IconSelector", icon_sel)
    for bad_key in ("  ", "My-Type", "battery"):
        res_bad = await flow.async_step_custom_type({
            CONF_TYPE_KEY: bad_key, CONF_TYPE_NAME_ZH: "测试类型",
            CONF_TYPE_THRESHOLD_TYPE: "lifetime_percent",
            CONF_TYPE_THRESHOLD: 20, CONF_TYPE_THRESHOLD_UNIT: "%",
        })
        check(f"类型键{bad_key!r}报错",
              res_bad["type"] == "form"
              and "type_key" in res_bad.get("errors", {}))
    res_ok = await flow.async_step_custom_type({
        CONF_TYPE_KEY: "my_type", CONF_TYPE_NAME_ZH: "我的类型",
        CONF_TYPE_ICON: "mdi:test-tube",
        CONF_TYPE_THRESHOLD_TYPE: "used_time",
        CONF_TYPE_THRESHOLD: 90, CONF_TYPE_THRESHOLD_UNIT: "days",
    })
    check("自定义类型创建条目", res_ok["type"] == "create_entry")
    check("自定义类型 entry_type 为键",
          res_ok["data"][CONF_ENTRY_TYPE] == "my_type")
    check("自定义类型标题为中文名", res_ok["title"] == "我的类型")
    user_lib = _user_lib(hass)
    mt = user_lib.get("types", {}).get("my_type")
    check("用户库含 my_type", mt is not None)
    check("用户库类型 name 为纯字符串", mt["name"] == "我的类型")
    check("用户库类型含图标与阈值",
          mt["icon"] == "mdi:test-tube"
          and mt["default_threshold_type"] == "used_time"
          and mt["default_threshold"] == 90
          and mt["default_threshold_unit"] == "days")
    from consumable_manager.library import load_library
    _lib = load_library()
    opt_values = [
        o["value"] for o in _build_entry_type_options(_lib, {}, "zh-Hans")
    ]
    check("下拉含 custom_type 入口", "custom_type" in opt_values)
    check("下拉含库存入口", ENTRY_TYPE_STOCK in opt_values)
    check("下拉含通知设置入口", ENTRY_TYPE_NOTIFICATION in opt_values)
    check("通知设置置顶", opt_values[0] == ENTRY_TYPE_NOTIFICATION)
    check("下拉选项数正确", len(opt_values) == 3 + len(_lib.types))

# ---- 选项界面（库存菜单 / 类型绑定）----
async def test_options_flow(hass, monkeypatch) -> None:
    _zh(hass)
    clean_persist(hass)
    _patch_translations(monkeypatch)
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK}, {})
    inject_entries(monkeypatch, hass, [stock_entry])
    oflow = _new_flow(ConsumableManagerOptionsFlow, hass, "stk1")
    res = await oflow.async_step_init()
    check("库存 init 为菜单", res["type"] == "menu")
    check("菜单含 add_item", "add_item" in res["menu_options"])
    res_menu = await oflow.async_step_add_item()
    check("添加库存为表单", res_menu["type"] == "form"
          and res_menu["step_id"] == "add_item")
    labels = await oflow._type_labels()
    check("关联类型无未关联", "none" not in labels)
    lib = await oflow._library()
    check("关联类型含库类型", set(labels) == set(lib.types))
    res_branch = await oflow.async_step_add_item({
        CONF_ITEM_TYPE: "filter", CONF_ADD_METHOD: ADD_METHOD_CONSUMABLE,
    })
    check("常用耗材分支", res_branch["type"] == "form"
          and res_branch["step_id"] == "consumable")
    check("前置类型已保存", oflow._item_type == "filter")
    res_branch2 = await oflow.async_step_add_item({
        CONF_ITEM_TYPE: "battery", CONF_ADD_METHOD: ADD_METHOD_CUSTOM_CONSUMABLE,
    })
    check("自定义耗材分支", res_branch2["type"] == "form"
          and res_branch2["step_id"] == "custom_stock_item")
    check("自定义前置类型已保存", oflow._item_type == "battery")
    library = await oflow._library()
    filtered = [c.id for c in oflow._consumables_for_type(library, "filter")]
    known_filters = {
        "filter_generic_pm25", "filter_hepa13",
        "filter_xiaomi_airpurifier_2",
        "filter_xiaomi_airpurifier_3",
        "filter_xiaomi_airpurifier_3c",
        "filter_xiaomi_airpurifier_4",
        "filter_xiaomi_airpurifier_4pro",
        "filter_xiaomi_airpurifier_max",
        "filter_xiaomi_airpurifier_proh",
    }
    check("按类型过滤耗材（已知子集）",
          known_filters.issubset(set(filtered)) and len(filtered) >= 9)
    check("未选类型显示全部",
          len(oflow._consumables_for_type(library, None)) > 2)
    res_s2 = await oflow.async_step_consumable({
        CONF_CONSUMABLE_ID: "filter_hepa13",
        CONF_QUANTITY: 2, CONF_STOCK_THRESHOLD: 1,
    })
    check("常用耗材保存", res_s2["type"] == "create_entry")
    item_c = res_s2["data"][CONF_STOCK_ITEMS][-1]
    check("耗材继承类型", item_c[CONF_ITEM_TYPE] == "filter")
    check("耗材继承名称", item_c[CONF_ITEM_NAME] == "HEPA 13 滤芯")
    check("耗材继承单位", item_c[CONF_UNIT] == "piece")
    check("耗材 id 已保存", item_c[CONF_CONSUMABLE_ID] == "filter_hepa13")
    new_item = {
        CONF_ITEM_NAME: "我的电池", CONF_ITEM_TYPE: "battery",
        CONF_MODEL: "CustomB1", CONF_UNIT: "个",
        CONF_QUANTITY: 3, CONF_STOCK_THRESHOLD: 1,
    }
    res2 = await oflow.async_step_custom_stock_item(new_item)
    check("自定义保存", res2["type"] == "create_entry")
    saved = res2["data"][CONF_STOCK_ITEMS]
    check("库存项已写入", len(saved) == 1)
    check("库存项名称", saved[0][CONF_ITEM_NAME] == "我的电池")
    check("库存项类型映射", saved[0][CONF_ITEM_TYPE] == "battery")
    check("库存项数量", saved[0][CONF_QUANTITY] == 3)
    check("自定义项有耗材 id", saved[0].get(CONF_CONSUMABLE_ID) == "battery_customb1")
    check("自定义项存型号", saved[0].get(CONF_MODEL) == "CustomB1")
    user_lib = _user_lib(hass)
    written = next(
        (c for c in user_lib.get("consumables", [])
         if c["id"] == "battery_customb1"),
        None,
    )
    check("用户库含自定义耗材", written is not None)
    check("用户库耗材字段正确",
          written["type"] == "battery"
          and written["model"] == "CustomB1"
          and written["name"] == "我的电池"
          and written["unit"] == "个")
    res3 = await oflow.async_step_custom_stock_item({
        CONF_ITEM_NAME: "我的电池2", CONF_ITEM_TYPE: "battery",
        CONF_MODEL: "CustomB1", CONF_UNIT: "个",
        CONF_QUANTITY: 1, CONF_STOCK_THRESHOLD: 1,
    })
    saved3 = res3["data"][CONF_STOCK_ITEMS]
    check("幂等 id 稳定",
          saved3[-1].get(CONF_CONSUMABLE_ID) == "battery_customb1")
    user_lib = _user_lib(hass)
    count = sum(
        1 for c in user_lib.get("consumables", [])
        if c["id"] == "battery_customb1"
    )
    check("用户库耗材不重复（幂等 upsert）", count == 1)
    res4 = await oflow.async_step_custom_stock_item({
        CONF_ITEM_NAME: "无单位电池", CONF_ITEM_TYPE: "battery",
        CONF_MODEL: "NoUnit", CONF_QUANTITY: 1, CONF_STOCK_THRESHOLD: 1,
    })
    check("缺单位保存成功", res4["type"] == "create_entry")
    user_lib = _user_lib(hass)
    written_nu = next(
        (c for c in user_lib.get("consumables", [])
         if c["id"] == "battery_nounit"),
        None,
    )
    check("缺单位用户库条目存在", written_nu is not None)
    check("缺单位兜底为 piece", written_nu["unit"] == "piece")
    oflow._library_cache = None  # 清缓存重新加载，读入刚写入的用户库
    lib_after = await oflow._library()
    check("缺单位自定义耗材可查",
          lib_after.get("battery_nounit") is not None
          and lib_after.get("battery_nounit").unit == "piece")
    res5 = await oflow.async_step_custom_stock_item({
        CONF_ITEM_NAME: "空单位电池", CONF_ITEM_TYPE: "battery",
        CONF_MODEL: "EmptyUnit", CONF_UNIT: "",
        CONF_QUANTITY: 1, CONF_STOCK_THRESHOLD: 1,
    })
    check("空单位被表单拒绝",
          res5.get("type") == "form"
          and res5.get("errors", {}).get(CONF_UNIT) == "required")
    stock_entry.options = {CONF_STOCK_ITEMS: saved}
    res3 = await oflow.async_step_init()
    check("有项后菜单含 select_item", "select_item" in res3["menu_options"])
    check("有项后菜单含 remove_items",
          "remove_items" in res3["menu_options"])
    check("通知渠道设置在菜单最末",
          res3["menu_options"][-1] == "notification")
    oflow._item_id = saved[0][CONF_ITEM_ID]
    res_edit_form = await oflow.async_step_edit_item()
    check("修改表单字段收敛",
          _schema_keys(res_edit_form["data_schema"])
          == {CONF_QUANTITY, CONF_STOCK_THRESHOLD})
    res_edit2 = await oflow.async_step_edit_item({
        CONF_QUANTITY: 5, CONF_STOCK_THRESHOLD: 2,
    })
    check("修改保存成功", res_edit2["type"] == "create_entry")
    edited = res_edit2["data"][CONF_STOCK_ITEMS][0]
    check("数量阈值已更新",
          edited[CONF_QUANTITY] == 5 and edited[CONF_STOCK_THRESHOLD] == 2)
    check("其余字段原样保留",
          edited[CONF_ITEM_NAME] == "我的电池"
          and edited.get(CONF_MODEL) == "CustomB1"
          and edited.get(CONF_CONSUMABLE_ID) == "battery_customb1")
    check("修改不重写用户库",
          len(_user_lib(hass).get("consumables", [])) == 2)
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"}, {})
    inject_entries(monkeypatch, hass, [stock_entry, type_entry])
    oflow2 = _new_flow(ConsumableManagerOptionsFlow, hass, "bat1")
    res4 = await oflow2.async_step_init()
    check("类型 init 为菜单", res4["type"] == "menu")
    check("菜单含 add_group", "add_group" in res4["menu_options"])
    check("空条目菜单不含 select_group",
          "select_group" not in res4["menu_options"])
    check("空条目菜单不含 remove_groups",
          "remove_groups" not in res4["menu_options"])
    check("菜单含 threshold", "threshold" in res4["menu_options"])
    check("菜单含 notification", "notification" in res4["menu_options"])
    # ---- 实体分组管理（多分组 → 多诊断实体）----
    legacy_entry = make_entry(
        "bat_legacy", "电池旧", {CONF_ENTRY_TYPE: "battery"},
        {CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.old"}]},
    )
    inject_entries(monkeypatch, hass,
                   [stock_entry, type_entry, legacy_entry])
    oflow_legacy = _new_flow(
        ConsumableManagerOptionsFlow, hass, "bat_legacy")
    legacy_groups = oflow_legacy._current_groups()
    check("旧扁平合成默认组",
          len(legacy_groups) == 1
          and legacy_groups[0][CONF_GROUP_ID] == "default"
          and legacy_groups[0][CONF_GROUP_NAME] == "电池旧")
    res_add = await oflow2.async_step_add_group()
    check("新增分组先选类别", res_add["type"] == "form"
          and res_add["step_id"] == "add_group")
    res_add2 = await oflow2.async_step_add_group(
        {CONF_GROUP_KIND: GROUP_KIND_BINDING})
    check("选绑定实体进入分组表单", res_add2["type"] == "form"
          and res_add2["step_id"] == "group")
    group_schema_keys = _schema_keys(res_add2["data_schema"])
    check("分组表单含分组名", CONF_GROUP_NAME in group_schema_keys)
    check("分组表单含实体", CONF_SOURCE_ENTITIES in group_schema_keys)
    res_g = await oflow2.async_step_group(user_input={
        CONF_GROUP_NAME: "客厅",
        CONF_ENTITY_REGEX: "",
        CONF_SOURCE_ENTITIES: ["sensor.battery_left", "sensor.battery_right"],
    })
    check("分组保存", res_g["type"] == "create_entry")
    saved_groups = res_g["data"][CONF_BINDING_GROUPS]
    check("落盘单分组", len(saved_groups) == 1)
    check("分组名正确", saved_groups[0][CONF_GROUP_NAME] == "客厅")
    check("分组 id 自动生成", bool(saved_groups[0][CONF_GROUP_ID]))
    saved_ids = sorted(
        s["entity_id"] for s in saved_groups[0][CONF_SOURCE_ENTITIES]
    )
    check("分组实体正确", saved_ids == [
        "sensor.battery_left", "sensor.battery_right",
    ])
    check("清理旧扁平键", CONF_SOURCE_ENTITIES not in res_g["data"])
    check("分组存正则规则(留空)", saved_groups[0].get(CONF_ENTITY_REGEX) == "")
    res_g2 = await oflow2.async_step_group(user_input={
        CONF_GROUP_NAME: "卧室",
        CONF_ENTITY_REGEX: "",
        CONF_SOURCE_ENTITIES: ["sensor.battery_bedroom"],
        "override_threshold": True,
        CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
        CONF_THRESHOLD: 3,
        CONF_THRESHOLD_UNIT: UNIT_DAYS,
        CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
    })
    g2 = res_g2["data"][CONF_BINDING_GROUPS][0]
    check("分组阈值覆盖写入",
          g2.get(CONF_THRESHOLD) == 3
          and g2.get(CONF_THRESHOLD_TYPE) == THRESHOLD_TYPE_REMAINING_TIME
          and g2.get(CONF_THRESHOLD_UNIT) == UNIT_DAYS)
    check("分组2存正则规则(留空)", g2.get(CONF_ENTITY_REGEX) == "")
    oflow2.config_entry.options = {
        CONF_BINDING_GROUPS: [{
            CONF_GROUP_ID: "g1", CONF_GROUP_NAME: "旧名",
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.x"}],
        }],
    }
    res_sel = await oflow2.async_step_select_group(
        {CONF_SELECTED_GROUP: "g1"})
    check("选择分组进入编辑表单", res_sel["type"] == "form"
          and res_sel["step_id"] == "group")
    res_edit = await oflow2.async_step_group(user_input={
        CONF_GROUP_NAME: "新名",
        CONF_ENTITY_REGEX: "",
        CONF_SOURCE_ENTITIES: ["sensor.x", "sensor.y"],
    })
    edited = res_edit["data"][CONF_BINDING_GROUPS][0]
    check("编辑分组名更新", edited[CONF_GROUP_NAME] == "新名")
    check("编辑分组 id 不变", edited[CONF_GROUP_ID] == "g1")
    check("编辑分组实体更新", sorted(
        s["entity_id"] for s in edited[CONF_SOURCE_ENTITIES]
    ) == ["sensor.x", "sensor.y"])
    oflow2.config_entry.options = {
        CONF_BINDING_GROUPS: [
            {CONF_GROUP_ID: "g1", CONF_GROUP_NAME: "甲",
             CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.a"}]},
            {CONF_GROUP_ID: "g2", CONF_GROUP_NAME: "乙",
             CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.b"}]},
        ],
    }
    await oflow2.async_step_select_group({CONF_SELECTED_GROUP: "g1"})
    res_real_edit = await oflow2.async_step_group({
        CONF_GROUP_NAME: "甲改",
        CONF_ENTITY_REGEX: "",
        CONF_SOURCE_ENTITIES: ["sensor.a", "sensor.c"],
    })
    real_groups = res_real_edit["data"][CONF_BINDING_GROUPS]
    check("HA 流程编辑不追加（组数仍为 2）", len(real_groups) == 2)
    check("HA 流程编辑命中 g1", real_groups[0][CONF_GROUP_ID] == "g1"
          and real_groups[0][CONF_GROUP_NAME] == "甲改")
    check("HA 流程编辑保留 g2", real_groups[1][CONF_GROUP_NAME] == "乙")
    oflow2.config_entry.options = {
        CONF_BINDING_GROUPS: [{
            CONF_GROUP_ID: "g1", CONF_GROUP_NAME: "待删",
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.x"}],
        }],
    }
    res_del = await oflow2.async_step_remove_groups(
        {CONF_REMOVE_GROUPS: ["g1"]})
    check("删除分组", res_del["type"] == "create_entry"
          and CONF_BINDING_GROUPS not in res_del["data"])
    oflow2.config_entry.options = {
        CONF_BINDING_GROUPS: [{
            CONF_GROUP_ID: "g1", CONF_GROUP_NAME: "甲",
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.a"}],
        }],
    }
    res_init2 = await oflow2.async_step_init()
    check("init 菜单含阈值项", "threshold" in res_init2.get("menu_options", []))
    check("有分组菜单含 select_group",
          "select_group" in res_init2["menu_options"])
    check("有分组菜单含 remove_groups",
          "remove_groups" in res_init2["menu_options"])
    res_t = await oflow2.async_step_threshold({
        CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
        CONF_THRESHOLD: 3,
        CONF_THRESHOLD_UNIT: UNIT_DAYS,
        CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
    })
    check("threshold 保存", res_t["type"] == "create_entry")
    saved_th = res_t["data"]
    check("阈值类型为 remaining_time",
          saved_th[CONF_THRESHOLD_TYPE] == THRESHOLD_TYPE_REMAINING_TIME)
    check("阈值为 3", saved_th[CONF_THRESHOLD] == 3)
    check("单位为天", saved_th[CONF_THRESHOLD_UNIT] == UNIT_DAYS)
    check("计算方式为小于",
          saved_th[CONF_THRESHOLD_OPERATOR] == OPERATOR_LESS_THAN)

# ---- 通知（全局条目直达表单 + 条目级覆盖开关语义）----
async def test_options_flow_notification(hass, monkeypatch) -> None:
    _zh(hass)
    clean_persist(hass)
    _patch_translations(monkeypatch)
    from consumable_manager.user_library import async_load_library
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: ["notify.phone"],
        }},
    )
    inject_entries(monkeypatch, hass, [notify_entry])
    library = await async_load_library(hass)
    options = _build_entry_type_options(library, {}, "zh-Hans")
    check("添加下拉含通知设置",
          any(o["value"] == ENTRY_TYPE_NOTIFICATION for o in options))
    of_ntf = _new_flow(ConsumableManagerOptionsFlow, hass, "ntf1")
    res = await of_ntf.async_step_init()
    check("通知条目直达表单", res["type"] == "form"
          and res["step_id"] == "notification")
    ntf_keys = _schema_keys(res["data_schema"])
    check("全局表单无 customize 开关",
          CONF_NOTIFY_CUSTOMIZE not in ntf_keys)
    check("全局表单含模式/样式/时刻",
          CONF_NOTIFY_MODE in ntf_keys
          and CONF_NOTIFY_STYLE in ntf_keys
          and CONF_NOTIFY_SCHEDULE_TIME in ntf_keys)
    _m, time_val = _schema_field(res["data_schema"], CONF_NOTIFY_SCHEDULE_TIME)
    check("时间字段为可空选择器 vol.Any(None, …)",
          hasattr(time_val, "validators")
          and len(time_val.validators) == 2
          and time_val.validators[0] is None)
    res_err = await of_ntf.async_step_notification({
        CONF_NOTIFY_SYSTEM: False,
        CONF_NOTIFY_ENTITIES: [],
    })
    check("无渠道报错", res_err.get("errors", {}).get("base") == "no_channel")
    res_ok = await of_ntf.async_step_notification({
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: ["notify.phone"],
    })
    check("全局配置保存", res_ok["type"] == "create_entry"
          and res_ok["data"][CONF_NOTIFICATION][CONF_NOTIFY_ENTITIES]
          == ["notify.phone"])
    saved_ntf = res_ok["data"][CONF_NOTIFICATION]
    check("全局缺省实时与人性化",
          saved_ntf[CONF_NOTIFY_MODE] == NOTIFY_MODE_REALTIME
          and saved_ntf[CONF_NOTIFY_STYLE] == NOTIFY_STYLE_HUMAN)
    check("实时保存时刻为空", saved_ntf[CONF_NOTIFY_SCHEDULE_TIME] == "")
    res_rt_time = await of_ntf.async_step_notification({
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: ["notify.phone"],
        CONF_NOTIFY_MODE: NOTIFY_MODE_REALTIME,
        CONF_NOTIFY_SCHEDULE_TIME: "20:00",
    })
    check("实时模式忽略时间",
          res_rt_time["data"][CONF_NOTIFICATION][CONF_NOTIFY_SCHEDULE_TIME]
          == "")
    res_sched = await of_ntf.async_step_notification({
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: ["notify.phone"],
        CONF_NOTIFY_MODE: NOTIFY_MODE_SCHEDULED,
        CONF_NOTIFY_SCHEDULE_TIME: "20:00",
        CONF_NOTIFY_STYLE: NOTIFY_STYLE_VALUE,
    })
    saved2 = res_sched["data"][CONF_NOTIFICATION]
    check("定时配置保存",
          saved2[CONF_NOTIFY_MODE] == NOTIFY_MODE_SCHEDULED
          and saved2[CONF_NOTIFY_SCHEDULE_TIME] == "20:00"
          and saved2[CONF_NOTIFY_STYLE] == NOTIFY_STYLE_VALUE)
    res_empty = await of_ntf.async_step_notification({
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: ["notify.phone"],
        CONF_NOTIFY_MODE: NOTIFY_MODE_SCHEDULED,
        CONF_NOTIFY_SCHEDULE_TIME: None,
    })
    check("定时留空时间存空串",
          res_empty["data"][CONF_NOTIFICATION][CONF_NOTIFY_SCHEDULE_TIME]
          == "")
    res_t = await of_ntf.async_step_notification({
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: [],
        CONF_NOTIFY_MODE: NOTIFY_MODE_SCHEDULED,
        CONF_NOTIFY_SCHEDULE_TIME: datetime.time(7, 30),
    })
    check("datetime.time 规范化为 HH:MM",
          res_t["data"][CONF_NOTIFICATION][CONF_NOTIFY_SCHEDULE_TIME]
          == "07:30")
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {
            CONF_STOCK_ITEMS: [{
                CONF_ITEM_ID: "a", CONF_ITEM_NAME: "滤芯",
                CONF_ITEM_TYPE: "filter", CONF_QUANTITY: 5,
                CONF_UNIT: "个", CONF_STOCK_THRESHOLD: 2,
            }],
            CONF_NOTIFICATION: {
                CONF_NOTIFY_SYSTEM: True,
                CONF_NOTIFY_ENTITIES: [],
                CONF_NOTIFY_STYLE: NOTIFY_STYLE_VALUE,
            },
        },
    )
    inject_entries(monkeypatch, hass, [notify_entry, stock_entry])
    of_stk = _new_flow(ConsumableManagerOptionsFlow, hass, "stk1")
    res_menu = await of_stk.async_step_init()
    check("库存菜单含通知", res_menu["type"] == "menu"
          and "notification" in res_menu["menu_options"])
    res_notify_form = await of_stk.async_step_notification()
    stk_keys = _schema_keys(res_notify_form["data_schema"])
    check("条目级表单含 customize 开关",
          CONF_NOTIFY_CUSTOMIZE in stk_keys)
    check("条目级表单含模式与时刻",
          CONF_NOTIFY_MODE in stk_keys
          and CONF_NOTIFY_SCHEDULE_TIME in stk_keys)
    res_off = await of_stk.async_step_notification({
        CONF_NOTIFY_CUSTOMIZE: False,
        CONF_NOTIFY_SYSTEM: False,
        CONF_NOTIFY_ENTITIES: [],
    })
    check("关闭 customize 回退全局",
          res_off["type"] == "create_entry"
          and CONF_NOTIFICATION not in res_off["data"])
    res_on = await of_stk.async_step_notification({
        CONF_NOTIFY_CUSTOMIZE: True,
        CONF_NOTIFY_SYSTEM: False,
        CONF_NOTIFY_ENTITIES: ["notify.pad"],
        CONF_NOTIFY_STYLE: NOTIFY_STYLE_VALUE,
        CONF_NOTIFY_MODE: NOTIFY_MODE_SCHEDULED,
        CONF_NOTIFY_SCHEDULE_TIME: "21:30",
    })
    check("开启 customize 保存条目级段",
          res_on["type"] == "create_entry"
          and res_on["data"][CONF_NOTIFICATION][CONF_NOTIFY_ENTITIES]
          == ["notify.pad"]
          and res_on["data"][CONF_NOTIFICATION][CONF_NOTIFY_STYLE]
          == NOTIFY_STYLE_VALUE
          and res_on["data"][CONF_NOTIFICATION][CONF_NOTIFY_MODE]
          == NOTIFY_MODE_SCHEDULED
          and res_on["data"][CONF_NOTIFICATION][CONF_NOTIFY_SCHEDULE_TIME]
          == "21:30")

async def test_entry_sort_prefix(hass, monkeypatch) -> None:
    """条目置顶：库存 / 通知标题加图标前缀，展示侧剥离。"""
    _zh(hass)
    _patch_translations(monkeypatch)
    from consumable_manager.library import load_library
    lib = load_library()
    stock_title = await async_entry_type_title(hass, ENTRY_TYPE_STOCK)
    ntf_title = await async_entry_type_title(hass, ENTRY_TYPE_NOTIFICATION)
    type_title = await async_entry_type_title(hass, "battery", lib)
    check("库存标题带前缀", stock_title.startswith("\U0001F4E6 "))
    check("通知标题带前缀", ntf_title.startswith("\u23F0 "))
    check("类型标题无前缀", not type_title.startswith("\U0001F4E6")
          and not type_title.startswith("\u23F0"))
    check("前缀顺序通知先于库存",
          ENTRY_SORT_PREFIXES[ENTRY_TYPE_NOTIFICATION]
          < ENTRY_SORT_PREFIXES[ENTRY_TYPE_STOCK])
    entry = make_entry(
        "stk9", "\U0001F4E6 耗材库存管理",
        {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK}, {CONF_STOCK_ITEMS: []},
    )
    coord = StockCoordinator(hass, entry, LABELS)
    check("协调器 title 剥离前缀", coord.title == "耗材库存管理")
    check("设备名剥离前缀", coord.device_info["name"] == "耗材库存管理")
    type_entry = make_entry(
        "bat9", "\u23F0 电池", {CONF_ENTRY_TYPE: "battery"}, {},
    )
    type_coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    check("类型协调器 title 剥离前缀", type_coord.title == "电池")
    plain = make_entry("bat8", "滤芯", {CONF_ENTRY_TYPE: "filter"}, {})
    check("无前缀标题原样", ConsumableTypeCoordinator(
        hass, plain, LABELS).title == "滤芯")

async def test_custom_entity_flow(hass, monkeypatch) -> None:
    """配置流：新建分组选「自定义耗材实体」→ custom_entity 表单 → 落盘 + 绑定入 Store。"""
    _zh(hass)
    clean_persist(hass)
    _patch_translations(monkeypatch)
    entry = make_entry("batx", "电池", {CONF_ENTRY_TYPE: "battery"}, {})
    inject_entries(monkeypatch, hass, [entry])
    oflow = _new_flow(ConsumableManagerOptionsFlow, hass, "batx")
    res_kind = await oflow.async_step_add_group(
        {CONF_GROUP_KIND: GROUP_KIND_CUSTOM})
    check("选自定义进入 custom_entity 表单",
          res_kind["type"] == "form" and res_kind["step_id"] == "custom_entity")
    res_save = await oflow.async_step_custom_entity({
        CONF_GROUP_NAME: "马桶遥控器电池",
        CONF_ADDED_AT: "2026-08-01",
        CONF_LIFESPAN: 180,
        CONF_LIFESPAN_UNIT: UNIT_DAYS,
        "override_threshold": True,
        CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
        CONF_THRESHOLD: 0,
        CONF_THRESHOLD_UNIT: UNIT_DAYS,
        CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
    })
    check("custom 保存", res_save["type"] == "create_entry")
    saved = res_save["data"][CONF_BINDING_GROUPS]
    check("落盘单 custom 分组", len(saved) == 1)
    g = saved[0]
    check("分组 kind=custom", g.get(CONF_GROUP_KIND) == GROUP_KIND_CUSTOM)
    check("分组名=名称", g[CONF_GROUP_NAME] == "马桶遥控器电池")
    check("分组 added_at 写入", g[CONF_ADDED_AT] == "2026-08-01")
    check("分组 lifespan 写入", g.get(CONF_LIFESPAN) == 180)
    check("分组 lifespan_unit 写入", g.get(CONF_LIFESPAN_UNIT) == UNIT_DAYS)
    check("阈值类型=剩余时间",
          g[CONF_THRESHOLD_TYPE] == THRESHOLD_TYPE_REMAINING_TIME)
    check("阈值算子=小于",
          g[CONF_THRESHOLD_OPERATOR] == OPERATOR_LESS_THAN)
    check("阈值=0（到期提醒）", g[CONF_THRESHOLD] == 0)
    check("无绑定实体键(绑走入Store)", CONF_CONSUMABLE_ID not in g)
    check("无实体清单键", CONF_SOURCE_ENTITIES not in g)
    gid = g[CONF_GROUP_ID]
    syn_id = custom_consumable_entity_id("batx", gid)
    check("合成实体 id 确定",
          syn_id == "sensor.consumable_manager_batx_" + gid + "_custom")
    oflow.config_entry.options = {CONF_BINDING_GROUPS: saved}
    res_sel = await oflow.async_step_select_group({CONF_SELECTED_GROUP: gid})
    check("修改 custom 路由到 custom_entity",
          res_sel["type"] == "form" and res_sel["step_id"] == "custom_entity")
    res_del = await oflow.async_step_remove_groups({CONF_REMOVE_GROUPS: [gid]})
    check("删除 custom 分组",
          res_del["type"] == "create_entry"
          and CONF_BINDING_GROUPS not in res_del["data"])
    res_bind = await oflow.async_step_custom_entity({
        CONF_GROUP_NAME: "客厅电池", CONF_ADDED_AT: "2026-08-01",
        CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
        "override_threshold": True,
        CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
        CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
        CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
        CONF_CONSUMABLE_ID: "battery_cr2032",
    })
    check("绑定耗材保存", res_bind["type"] == "create_entry")
    gb = res_bind["data"][CONF_BINDING_GROUPS][0]
    check("分组不存耗材 id（走Store）",
          gb.get(CONF_CONSUMABLE_ID) is None)
    syn_b = custom_consumable_entity_id("batx", gb[CONF_GROUP_ID])
    check("绑定写入 Store 层",
          bindings.get_binding(oflow.hass, syn_b) == "battery_cr2032")
    library = await oflow._library()
    labels = await oflow._type_labels()
    opts = oflow._consumable_options(
        oflow._consumables_for_type(library, "battery"), labels,
        hass.config.language)
    check("本类型下拉仅含 battery 耗材",
          all(c.startswith("battery_") for c in (o["value"] for o in opts)))
    check("本类型下拉不含 filter 类型",
          not any(o["value"] == "filter_hepa13" for o in opts))
    oflow.config_entry.options = {}
    res_err = await oflow.async_step_custom_entity({
        CONF_GROUP_NAME: "", CONF_ADDED_AT: "",
        CONF_LIFESPAN: None,
        CONF_LIFESPAN_UNIT: UNIT_DAYS,
        "override_threshold": True,
        CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
        CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
        CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
    })
    check("缺名称/时间/寿命报错",
          res_err["type"] == "form" and res_err.get("errors"))

async def test_custom_entity_form_default_is_selectable(hass, monkeypatch) -> None:
    """custom_entity 的耗材下拉：只有合法选项值才可作为 default。"""
    _zh(hass)
    clean_persist(hass)
    _patch_translations(monkeypatch)
    entry = make_entry("batd", "电池", {CONF_ENTRY_TYPE: "battery"}, {})
    inject_entries(monkeypatch, hass, [entry])
    oflow = _new_flow(ConsumableManagerOptionsFlow, hass, "batd")
    res_new = await oflow.async_step_custom_entity()
    check("新建返回表单", res_new["type"] == "form")
    has_def, val = _field_default(
        res_new["data_schema"], CONF_CONSUMABLE_ID)
    check("未绑定时耗材下拉无默认值", not has_def)
    async def _edit_form(gid, consumable_id):
        """建一个 custom 分组 + Store 绑定，返回其编辑表单。"""
        oflow.config_entry.options = {
            CONF_BINDING_GROUPS: [{
                CONF_GROUP_ID: gid, CONF_GROUP_NAME: "遥控器电池",
                CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                CONF_ADDED_AT: "2026-08-01",
                CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
            }],
        }
        await bindings.async_set_binding(
            oflow.hass, custom_consumable_entity_id("batd", gid),
            consumable_id)
        return await oflow.async_step_select_group(
            {CONF_SELECTED_GROUP: gid})
    res_ok = await _edit_form("good", "battery_cr2032")
    check("编辑返回 custom_entity 表单",
          res_ok["type"] == "form" and res_ok["step_id"] == "custom_entity")
    has_def, val = _field_default(
        res_ok["data_schema"], CONF_CONSUMABLE_ID)
    check("合法绑定时保留回填", has_def and val == "battery_cr2032")
    await _edit_form("dangling", "battery_no_longer_exists")
    res_dangle = await oflow.async_step_custom_entity()
    check("悬空编辑返回表单",
          res_dangle["type"] == "form"
          and res_dangle["step_id"] == "custom_entity")
    has_def, val = _field_default(
        res_dangle["data_schema"], CONF_CONSUMABLE_ID)
    check("悬空绑定时耗材下拉无默认值", not has_def)
