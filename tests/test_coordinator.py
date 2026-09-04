"""阈值评估 / 协调器 / 待办联动 / sensor·todo 实体。"""

from __future__ import annotations
from homeassistant.config_entries import ConfigEntryState
from _helpers import LABELS, check, inject_entries, make_entry
from consumable_manager.coordinator import (
    REPLACE_STATES,
    STOCK_STATES,
    STATE_OK,
    STATE_LOW_STOCK,
    STATE_REPLACE_NEEDED,
    TODO_STATUS_COMPLETED,
    TODO_STATUS_NEEDS_ACTION,
    ConsumableManagerData,
    ConsumableTypeCoordinator,
    StockCoordinator,
    evaluate_threshold,

)

from consumable_manager.sensor import (
    REPLACE_STATUS_DESCRIPTION,
    STOCK_STATUS_DESCRIPTION,
    ReplaceStatusSensor,
    StockItemSensor,
    StockStatusSensor,
    build_item_description,

)

from consumable_manager.todo import (
    ConsumableTodoListEntity,

)

from consumable_manager.const import (
    CONF_ENTRY_TYPE,
    CONF_ITEM_ID,
    CONF_LAST_REPLACED,
    CONF_ITEM_NAME,
    CONF_ITEM_TYPE,
    CONF_QUANTITY,
    CONF_SOURCE_ENTITIES,
    CONF_STOCK_ITEMS,
    CONF_STOCK_THRESHOLD,
    CONF_THRESHOLD,
    CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT,
    CONF_UNIT,
    DOMAIN,
    ENTRY_TYPE_STOCK,
    OPERATOR_EQUAL,
    OPERATOR_GREATER_THAN,
    OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_LIFETIME_PERCENT,
    THRESHOLD_TYPE_REMAINING_TIME,
    THRESHOLD_TYPE_USED_TIME,
    TODO_KIND_REPLACE,
    UNIT_DAYS,
    UNIT_HOURS,
    UNIT_MINUTES,

)

# ---- 用例 ----
def test_evaluate_threshold() -> None:
    cases = [
        (THRESHOLD_TYPE_LIFETIME_PERCENT, 20, OPERATOR_LESS_THAN, [15.0], None, True),
        (THRESHOLD_TYPE_LIFETIME_PERCENT, 20, OPERATOR_LESS_THAN, [80.0], None, False),
        (THRESHOLD_TYPE_LIFETIME_PERCENT, 20, OPERATOR_LESS_THAN, [None], None, False),
        (THRESHOLD_TYPE_REMAINING_TIME, 24, OPERATOR_LESS_THAN, [10.0], UNIT_HOURS, True),
        (THRESHOLD_TYPE_REMAINING_TIME, 24, OPERATOR_LESS_THAN, [30.0], UNIT_HOURS, False),
        (THRESHOLD_TYPE_USED_TIME, 720, OPERATOR_GREATER_THAN, [800.0], UNIT_HOURS, True),
        (THRESHOLD_TYPE_USED_TIME, 720, OPERATOR_GREATER_THAN, [700.0], UNIT_HOURS, False),
        (THRESHOLD_TYPE_LIFETIME_PERCENT, None, OPERATOR_LESS_THAN, [15.0], None, False),
        (THRESHOLD_TYPE_LIFETIME_PERCENT, 20, OPERATOR_LESS_THAN, [80.0, 15.0], None, True),
        (THRESHOLD_TYPE_REMAINING_TIME, 1, OPERATOR_LESS_THAN, [20.0], UNIT_DAYS, True),
        (THRESHOLD_TYPE_REMAINING_TIME, 1, OPERATOR_LESS_THAN, [30.0], UNIT_DAYS, False),
        (THRESHOLD_TYPE_USED_TIME, 30, OPERATOR_GREATER_THAN, [1.0], UNIT_MINUTES, True),
        (THRESHOLD_TYPE_REMAINING_TIME, 24, OPERATOR_EQUAL, [24.0], UNIT_HOURS, True),
        (THRESHOLD_TYPE_REMAINING_TIME, 24, OPERATOR_EQUAL, [25.0], UNIT_HOURS, False),
    ]
    for tt, thr, op, vals, unit, expected in cases:
        got = evaluate_threshold(tt, thr, op, vals, unit)
        check(f"评估 {tt}/{thr}/{op}/{vals}/{unit} \u2192 {expected}",
              got == expected)

# ---- 库存协调器 ----
async def test_stock_coordinator(hass, monkeypatch) -> None:
    item_a = {
        CONF_ITEM_ID: "a", CONF_ITEM_NAME: "电池", CONF_ITEM_TYPE: "battery",
        CONF_QUANTITY: 5, CONF_UNIT: "个", CONF_STOCK_THRESHOLD: 2,
    }
    item_b = {
        CONF_ITEM_ID: "b", CONF_ITEM_NAME: "滤芯", CONF_ITEM_TYPE: None,
        CONF_QUANTITY: 0, CONF_UNIT: "片", CONF_STOCK_THRESHOLD: 1,
    }
    entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [item_a, item_b]},
    )
    inject_entries(monkeypatch, hass, [entry])
    coord = StockCoordinator(hass, entry, LABELS)
    di = coord.device_info
    check("device_info identifiers",
          di["identifiers"] == {("consumable_manager", "stk1")})
    check("device_info name", di["name"] == "库存")
    check("device_info model", di["model"] == ENTRY_TYPE_STOCK)
    check("item_ids", coord.item_ids == ("a", "b"))
    check("quantity a", coord.quantity("a") == 5)
    check("quantity b", coord.quantity("b") == 0)
    check("unit a", coord.unit("a") == "个")
    check("is_low a (5>=2)", coord.is_low("a") is False)
    check("is_low b (0<1)", coord.is_low("b") is True)
    check("low_items", coord.low_items() == ["b"])
    check("entity_signature 含 stock_status",
          coord.entity_signature == ("a", "b", "stock_status"))
    check("items_for_type battery", coord.items_for_type("battery") == ["a"])
    check("stock_status = 库存不足", coord.stock_status == STATE_LOW_STOCK)
    coord.async_set_quantity("a", 1)
    check("set_quantity 写入 options",
          entry.options[CONF_STOCK_ITEMS][0][CONF_QUANTITY] == 1)
    coord.async_add_quantity("a", 2)  # 1+2=3
    check("add_quantity", coord.quantity("a") == 3)
    await coord._async_refresh()
    todos = coord.todo_dicts()
    purchase = [t for t in todos if "_purchase_" in t["uid"]]
    check("低库存项各一条购买待办", len(purchase) == 1)
    pb = next(t for t in purchase if t["uid"].endswith("_purchase_b"))
    check("购买待办状态为待处理",
          pb["status"] == TODO_STATUS_NEEDS_ACTION)
    check("购买待办摘要为单个库存项",
          pb["summary"] == "购买 滤芯"
          and "电池" not in pb["summary"])
    check("购买待办描述含数量与阈值",
          (pb.get("description") or "")
          and "滤芯" in pb["description"]
          and "阈值" in pb["description"])
    coord.async_add_quantity("b", 5)
    await coord._async_refresh()
    todos = coord.todo_dicts()
    purchase = [t for t in todos if "_purchase_" in t["uid"]]
    check("补货后购买待办完成",
          len(purchase) == 1
          and purchase[0]["status"] == TODO_STATUS_COMPLETED)
    coord.async_set_quantity("a", 0)
    coord.async_set_quantity("b", 0)
    await coord._async_refresh()
    purchase = [t for t in coord.todo_dicts()
                if "_purchase_" in t["uid"]]
    check("多项低库存各自独立一条", len(purchase) == 2)
    check("滤芯待办摘要",
          any(t["uid"].endswith("_purchase_b")
              and t["summary"] == "购买 滤芯" for t in purchase))
    check("电池待办摘要",
          any(t["uid"].endswith("_purchase_a")
              and t["summary"] == "购买 电池" for t in purchase))
    coord.async_set_quantity("a", 3)
    coord.async_set_quantity("b", 5)
    coord.async_upsert_todo(uid=None, summary="手动待办",
                            status=TODO_STATUS_NEEDS_ACTION)
    check("手动+自动待办并存", len(coord.todo_dicts()) == 3)
    custom = [t for t in coord.todo_dicts() if "_custom_" in t["uid"]][0]
    coord.async_delete_todos({custom["uid"]})
    check("删除手动待办后剩自动待办", len(coord.todo_dicts()) == 2)
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    await coord.async_config_entry_first_refresh()
    check("首次刷新可用", coord.stock_status == STATE_OK)

# ---- 耗材类型协调器（含 mark_replaced 联动扣库存）----
async def test_consumable_type_coordinator(hass, monkeypatch) -> None:
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.x_battery", "device_name": "灯",
                "device_model": "A", "manufacturer": "B",
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    stock_item = {
        CONF_ITEM_ID: "bat_stock", CONF_ITEM_NAME: "备用电池",
        CONF_ITEM_TYPE: "battery", CONF_QUANTITY: 5, CONF_UNIT: "个",
        CONF_STOCK_THRESHOLD: 1,
    }
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [stock_item]},
    )
    inject_entries(monkeypatch, hass, [stock_entry, type_entry])
    stock_coord = StockCoordinator(hass, stock_entry, LABELS)
    stock_entry.runtime_data = ConsumableManagerData(coordinator=stock_coord)
    type_coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    type_entry.runtime_data = ConsumableManagerData(coordinator=type_coord)
    check("cons_type", type_coord.cons_type == "battery")
    check("entity_signature 含诊断 + 数据传感器（默认分组非自定义）",
          type_coord.entity_signature == (
              "replace_status:default", "group_entity_data:default"))
    check("source_entities",
          type_coord.source_entities == ["sensor.x_battery"])
    hass.states.async_set("sensor.x_battery", "15.0")
    check("实体值低 → 需要更换",
          type_coord.replace_status == STATE_REPLACE_NEEDED)
    hass.states.async_set("sensor.x_battery", "80.0")
    check("实体值高 → 正常", type_coord.replace_status == STATE_OK)
    type_coord.async_mark_replaced()
    check("last_replaced 已记录",
          type_entry.options[CONF_LAST_REPLACED] is not None)
    check("联动扣库存 5→4", stock_coord.quantity("bat_stock") == 4)

async def test_todo_check_marks_replaced(hass, monkeypatch) -> None:
    """勾选「更换」待办 = 已更换：记录时间 + 联动扣库存。"""
    stock_item = {
        CONF_ITEM_ID: "bat_stock", CONF_ITEM_NAME: "备用电池",
        CONF_ITEM_TYPE: "battery", CONF_QUANTITY: 5, CONF_UNIT: "个",
        CONF_STOCK_THRESHOLD: 1,
    }
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [stock_item]},
    )
    type_entry = make_entry("bat1", "电池", {CONF_ENTRY_TYPE: "battery"}, {})
    inject_entries(monkeypatch, hass, [stock_entry, type_entry])
    stock_coord = StockCoordinator(hass, stock_entry, LABELS)
    stock_entry.runtime_data = ConsumableManagerData(coordinator=stock_coord)
    from consumable_manager.library import load_library
    hass.config.language = "zh-Hans"
    type_coord = ConsumableTypeCoordinator(
        hass, type_entry, LABELS, None, load_library()
    )
    type_entry.runtime_data = ConsumableManagerData(coordinator=type_coord)
    replace_uid = type_coord._auto_uid(TODO_KIND_REPLACE, "sensor.x_battery")
    type_coord._upsert_auto_todo(
        replace_uid, "更换 电池", TODO_STATUS_NEEDS_ACTION,
        type_coord._replace_description())
    desc0 = type_coord._todos[replace_uid].get("description") or ""
    check("无绑定实体→更换待办描述含耗材字段（未知）",
          "**耗材**：未知" in desc0)
    check("未更换过则无上次更换时间", "上次更换" not in desc0)
    type_coord.async_on_todo_completed(
        "custom_uid", TODO_STATUS_NEEDS_ACTION, TODO_STATUS_COMPLETED)
    check("非更换待办不记录 last_replaced",
          type_entry.options.get(CONF_LAST_REPLACED) is None)
    check("非更换待办不扣库存", stock_coord.quantity("bat_stock") == 5)
    type_coord.async_on_todo_completed(
        replace_uid, TODO_STATUS_NEEDS_ACTION, TODO_STATUS_COMPLETED)
    check("勾选更换待办记录 last_replaced",
          type_entry.options.get(CONF_LAST_REPLACED) is not None)
    check("勾选更换待办联动扣库存 5→4",
          stock_coord.quantity("bat_stock") == 4)
    check("更换待办描述记录上次更换时间",
          "**上次更换**：" in (type_coord._todos[replace_uid]
                             .get("description") or ""))
    type_coord.async_on_todo_completed(
        replace_uid, TODO_STATUS_COMPLETED, TODO_STATUS_COMPLETED)
    check("已完成待办不重复扣库存", stock_coord.quantity("bat_stock") == 4)

async def test_replace_todo_per_entity(hass, monkeypatch) -> None:
    """每个触发实体各一条「更换」待办；标题=设备名+请更换耗材。"""
    from consumable_manager.library import load_library
    library = load_library()
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [
                {"entity_id": "sensor.a", "device_name": "书房传感器"},
                {"entity_id": "sensor.b", "device_name": "客厅传感器"},
            ],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    inject_entries(monkeypatch, hass, [entry])
    hass.config.language = "zh-Hans"
    coord = ConsumableTypeCoordinator(hass, entry, LABELS, None, library)
    entry.runtime_data = ConsumableManagerData(coordinator=coord)
    hass.states.async_set("sensor.a", "10.0")
    hass.states.async_set("sensor.b", "15.0")
    await coord._async_refresh()
    a_uid = coord._auto_uid(TODO_KIND_REPLACE, "sensor.a")
    b_uid = coord._auto_uid(TODO_KIND_REPLACE, "sensor.b")
    todos = coord.todo_dicts()
    replace = [t for t in todos if t["uid"].startswith(
        coord._auto_uid(TODO_KIND_REPLACE))]
    check("每个触发实体一条更换待办", len(replace) == 2)
    check("标题=设备名+请更换耗材(书房)",
          any(t["summary"] == "书房传感器 请更换耗材。"
              for t in replace))
    check("标题=设备名+请更换耗材(客厅)",
          any(t["summary"] == "客厅传感器 请更换耗材。"
              for t in replace))
    check("待办均为待处理", all(t["status"] == "needs_action"
          for t in replace))
    hass.states.async_set("sensor.a", "90.0")
    await coord._async_refresh()
    todos = coord.todo_dicts()
    by_uid = {t["uid"]: t for t in todos}
    check("恢复实体待办自动完成", by_uid[a_uid]["status"] == "completed")
    check("仍触发实体待办保持待处理",
          by_uid[b_uid]["status"] == "needs_action")
    entry.options[CONF_SOURCE_ENTITIES] = [
        {"entity_id": "sensor.a", "device_name": "书房传感器"}]
    await coord._async_refresh()
    todos = coord.todo_dicts()
    check("解绑实体待办被清理",
          b_uid not in [t["uid"] for t in todos])

async def test_todo_title_friendly_name_priority(hass, monkeypatch) -> None:
    """待办标题/描述显示名：friendly_name 优先于设备型号/设备注册表名。"""
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [
                {
                    "entity_id": "sensor.lumi_motion",
                    "device_name": "LUMI lumi.sensor_motion",
                    "device_model": "LUMI",
                },
                {"entity_id": "sensor.temp_room",
                 "device_name": "Temp Sensor", "device_model": "TM200"},
            ],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    inject_entries(monkeypatch, hass, [entry])
    hass.config.language = "zh-Hans"
    coord = ConsumableTypeCoordinator(hass, entry, LABELS)
    hass.states.async_set(
        "sensor.lumi_motion", "15.0", {"friendly_name": "书房人体传感器"})
    hass.states.async_set("sensor.temp_room", "10.0")
    await coord._async_refresh()
    replace = [t for t in coord.todo_dicts()
               if t["uid"].startswith(coord._auto_uid(TODO_KIND_REPLACE))]
    check("friendly_name 测试：两个实体各一条", len(replace) == 2)
    replace_prefix = coord._auto_uid(TODO_KIND_REPLACE) + "_"
    by_eid = {t["uid"][len(replace_prefix):]: t for t in replace}
    check("标题用 friendly_name 而非型号",
          by_eid["sensor.lumi_motion"]["summary"]
          == "书房人体传感器 请更换耗材。")
    check("无 friendly_name 回退设备名",
          by_eid["sensor.temp_room"]["summary"]
          == "Temp Sensor 请更换耗材。")
    desc = by_eid["sensor.lumi_motion"].get("description") or ""
    check("描述设备行用 friendly_name", "书房人体传感器" in desc)

# ---- 实体 ----
async def test_entities(hass, monkeypatch) -> None:
    item = {
        CONF_ITEM_ID: "a", CONF_ITEM_NAME: "电池", CONF_ITEM_TYPE: "battery",
        CONF_QUANTITY: 3, CONF_UNIT: "个", CONF_STOCK_THRESHOLD: 1,
    }
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [item]},
    )
    inject_entries(monkeypatch, hass, [stock_entry])
    stock_coord = StockCoordinator(hass, stock_entry, LABELS)
    desc = build_item_description(item)
    type_icons = {"battery": "mdi:battery-outline"}
    stock_ent = StockItemSensor(
        stock_coord, desc, item.get(CONF_ITEM_TYPE), type_icons
    )
    check("库存项 unique_id", stock_ent._attr_unique_id == "stk1_a")
    check("库存项 item_id", stock_ent.item_id == "a")
    check("库存项名称", stock_ent.entity_description.name == "电池")
    check("库存项单位",
          stock_ent.entity_description.native_unit_of_measurement == "个")
    check("库存项值", stock_ent.native_value == 3)
    check("库存项图标(类型)", stock_ent.icon == "mdi:battery-outline")
    check("库存项属性含 item_id",
          stock_ent.extra_state_attributes["item_id"] == "a")
    stock_coord.async_set_quantity("a", -1)
    check("欠货图标警示", "remove" in stock_ent.icon)
    check("欠货值", stock_ent.native_value == -1)
    status_ent = StockStatusSensor(stock_coord, STOCK_STATUS_DESCRIPTION)
    check("汇总 unique_id", status_ent._attr_unique_id == "stk1_stock_status")
    check("汇总枚举设备类",
          status_ent.entity_description.device_class is not None)
    check("汇总枚举 options",
          status_ent.entity_description.options == list(STOCK_STATES))
    check("汇总值(欠货)", status_ent.native_value == STATE_LOW_STOCK)
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.x"}],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    type_coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    replace_ent = ReplaceStatusSensor(
        type_coord, REPLACE_STATUS_DESCRIPTION, type_coord.groups[0]
    )
    check("更换 unique_id", replace_ent._attr_unique_id == "bat1_grp_default")
    check("更换实体名=分组名", replace_ent._attr_name == "电池")
    check("更换枚举设备类",
          replace_ent.entity_description.device_class is not None)
    check("更换枚举 options",
          replace_ent.entity_description.options == list(REPLACE_STATES))
    hass.states.async_set("sensor.x", "15.0")
    check("更换值(低)", replace_ent.native_value == STATE_REPLACE_NEEDED)
    todo_ent = ConsumableTodoListEntity(stock_coord)
    check("todo 翻译键", todo_ent._attr_translation_key == "todo")
    check("todo 初始空", todo_ent.todo_items == [])
    stock_coord.async_upsert_todo(uid=None, summary="手动",
                                  status=TODO_STATUS_NEEDS_ACTION,
                                  description="手动备注")
    items = todo_ent.todo_items
    check("todo 转换数量", len(items) == 1)
    check("todo uid 非空", items[0].uid is not None)
    check("todo 描述带出", items[0].description == "手动备注")
