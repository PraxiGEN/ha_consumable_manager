"""群组展开 / 绑定分组 / 自定义实体 / 绑定层。"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from _helpers import (
    LABELS,
    check,
    clean_persist,
    inject_entries,
    make_entry,

)

from consumable_manager.coordinator import (
    STATE_OK,
    STATE_REPLACE_NEEDED,
    ConsumableManagerData,
    ConsumableTypeCoordinator,

)

from consumable_manager.coordinator import type as type_mod
from consumable_manager.sensor import (
    CustomConsumableSensor,
    GroupDataSensor,
    ReplaceStatusSensor,

)

from consumable_manager.const import (
    CONF_ENTITY_REGEX,
    CONF_ENTRY_TYPE,
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
    CONF_THRESHOLD,
    CONF_THRESHOLD_OPERATOR,
    CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT,
    OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_LIFETIME_PERCENT,
    THRESHOLD_TYPE_REMAINING_TIME,
    TODO_KIND_REPLACE,
    UNIT_DAYS,
    UNIT_PERCENT,

)

from consumable_manager import bindings

# ---- 用例 ----
async def test_group_expansion(hass, monkeypatch) -> None:
    """绑定群组：运行时展开成员评估；嵌套 / 去重 / 动态跟随 / 成员名称。"""
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "group.sensors", "device_name": None,
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    plain = make_entry(
        "bat2", "滤芯", {CONF_ENTRY_TYPE: "filter"},
        {
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.x"}],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    sensor_group = make_entry(
        "bat3", "电池组", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.wireless_battery_group",
                "device_name": None,
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    nested = make_entry(
        "bat4", "嵌套组", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.outer_group", "device_name": None,
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    inject_entries(monkeypatch, hass, [type_entry, plain, sensor_group, nested])
    coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    hass.states.async_set("group.sensors", "30", {
        "entity_id": ["sensor.a", "sensor.b", "group.sub"],
    })
    hass.states.async_set("group.sub", "40", {
        "entity_id": ["sensor.b", "sensor.c"],
    })
    for entity_id, value in (("sensor.a", "10"), ("sensor.b", "50"),
                             ("sensor.c", "15")):
        hass.states.async_set(
            entity_id, value, {"friendly_name": f"设备{entity_id[-1].upper()}"})
    check("群组递归展开去重保序",
          coord.resolved_source_entities()
          == ["sensor.a", "sensor.b", "sensor.c"])
    check("展开成员逐个评估", coord.replace_status == STATE_REPLACE_NEEDED)
    check("触发实体为展开成员",
          set(coord.triggered_entities()) == {"sensor.a", "sensor.c"})
    pairs = coord.triggered_pairs()
    names = {display for display, _value, _unit in pairs}
    check("成员显示名用实体名称", names == {"设备A", "设备C"})
    hass.states.async_set("group.sensors", "30", {"entity_id": ["sensor.a"]})
    await coord._async_refresh()
    check("群组变化动态跟随",
          coord.resolved_source_entities() == ["sensor.a"])
    coord2 = ConsumableTypeCoordinator(hass, plain, LABELS)
    hass.states.async_set("sensor.x", "15", {"friendly_name": "净水器滤芯"})
    check("普通绑定不展开",
          coord2.resolved_source_entities() == ["sensor.x"]
          and coord2.replace_status == STATE_REPLACE_NEEDED)
    coord3 = ConsumableTypeCoordinator(hass, sensor_group, LABELS)
    hass.states.async_set(
        "sensor.wireless_battery_group", "30",
        {"entity_id": ["sensor.w1", "sensor.w2"]})
    hass.states.async_set("sensor.w1", "10", {"friendly_name": "无线设备A"})
    hass.states.async_set("sensor.w2", "60", {"friendly_name": "无线设备B"})
    check("sensor 域群组同样展开成员",
          coord3.resolved_source_entities()
          == ["sensor.w1", "sensor.w2"])
    check("sensor 域群组逐个评估",
          coord3.triggered_entities() == ["sensor.w1"])
    check("sensor 域群组触发实体非组本身",
          "sensor.wireless_battery_group"
          not in coord3.triggered_entities())
    coord4 = ConsumableTypeCoordinator(hass, nested, LABELS)
    hass.states.async_set(
        "sensor.outer_group", "50",
        {"entity_id": ["sensor.mid_group", "sensor.leaf_a"]})
    hass.states.async_set(
        "sensor.mid_group", "60",
        {"entity_id": ["sensor.leaf_b", "sensor.deep_group"]})
    hass.states.async_set(
        "sensor.deep_group", "70", {"entity_id": ["sensor.leaf_c"]})
    for entity_id, value in (("sensor.leaf_a", "40"),
                             ("sensor.leaf_b", "10"),
                             ("sensor.leaf_c", "15")):
        hass.states.async_set(entity_id, value)
    check("sensor 域组中组递归展开到底",
          coord4.resolved_source_entities()
          == ["sensor.leaf_b", "sensor.leaf_c", "sensor.leaf_a"])
    check("组中组触发实体为最深层叶子",
          set(coord4.triggered_entities())
          == {"sensor.leaf_b", "sensor.leaf_c"})

async def test_group_trigger_generates_todos(hass, monkeypatch) -> None:
    """群组绑定 + 多成员触发：每个触发成员应生成独立待办（跑完整刷新管线）。"""
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "group.sensors", "device_name": None,
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    inject_entries(monkeypatch, hass, [type_entry])
    coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    hass.states.async_set(
        "group.sensors", "30", {"entity_id": ["sensor.a", "sensor.b"]})
    hass.states.async_set("sensor.a", "10", {"friendly_name": "设备A"})
    hass.states.async_set("sensor.b", "15", {"friendly_name": "设备B"})
    await coord._async_refresh()
    replace = [t for t in coord.todo_dicts()
               if "_replace_" in t["uid"]]
    check("群组多成员触发各生成待办", len(replace) == 2)
    summaries = {t["summary"] for t in replace}
    check("群组成员待办标题用设备名",
          "设备A 请更换耗材。" in summaries
          and "设备B 请更换耗材。" in summaries)
    captured: dict = {}
    def _fake_track(hass_, entities, handler):
        captured["entities"] = list(entities)
        captured["handler"] = handler
        def _unsub():
            captured["unsubbed"] = True
        return _unsub
    monkeypatch.setattr(
        type_mod, "async_track_state_change_event", _fake_track)
    unsub = coord.async_subscribe()
    check("群组订阅含展开成员",
          set(captured["entities"]) == {"group.sensors", "sensor.a", "sensor.b"})
    if unsub is not None:
        unsub()
    check("订阅返回可卸载", captured.get("unsubbed") is True)

async def test_binding_groups(hass, monkeypatch) -> None:
    """分组数据模型：groups 属性、entity_signature、逐组状态/属性、阈值覆盖。"""
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {
                    CONF_GROUP_ID: "living", CONF_GROUP_NAME: "客厅",
                    CONF_SOURCE_ENTITIES: [
                        {"entity_id": "sensor.bat_living"},
                    ],
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
                    CONF_THRESHOLD: 3, CONF_THRESHOLD_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
                },
                {
                    CONF_GROUP_ID: "bedroom", CONF_GROUP_NAME: "卧室",
                    CONF_SOURCE_ENTITIES: [
                        {"entity_id": "sensor.bat_bedroom"},
                    ],
                },
            ],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    inject_entries(monkeypatch, hass, [entry])
    coord = ConsumableTypeCoordinator(hass, entry, LABELS)
    groups = coord.groups
    check("groups 数量", len(groups) == 2)
    check("groups id 顺序",
          [g[CONF_GROUP_ID] for g in groups] == ["living", "bedroom"])
    check("entity_signature 多分组",
          coord.entity_signature == (
              "replace_status:living", "replace_status:bedroom",
              "group_entity_data:living", "group_entity_data:bedroom"))
    check("快照并集",
          sorted(s["entity_id"] for s in coord.source_snapshots) == [
              "sensor.bat_bedroom", "sensor.bat_living"])
    liv, bed = groups[0], groups[1]
    check("living 阈值覆盖",
          coord._group_threshold(liv) == (
              THRESHOLD_TYPE_REMAINING_TIME, 3.0, UNIT_DAYS,
              OPERATOR_LESS_THAN))
    check("bedroom 回退条目级",
          coord._group_threshold(bed) == (
              THRESHOLD_TYPE_LIFETIME_PERCENT, 20.0, UNIT_PERCENT,
              OPERATOR_LESS_THAN))
    hass.states.async_set(
        "sensor.bat_living", "24.0", {"unit_of_measurement": "h"})
    hass.states.async_set("sensor.bat_bedroom", "80.0")
    check("living 组触发", coord.group_status(liv) == STATE_REPLACE_NEEDED)
    check("bedroom 组正常", coord.group_status(bed) == STATE_OK)
    check("整体状态触发", coord.replace_status == STATE_REPLACE_NEEDED)
    attr = coord.group_attributes(liv)
    check("分组属性名", attr["group"] == "客厅")
    check("分组属性阈值",
          attr["threshold_type"] == THRESHOLD_TYPE_REMAINING_TIME
          and attr["threshold"] == 3.0)
    check("分组属性实体",
          attr["source_entities"] == ["sensor.bat_living"])
    check("分组属性触发列表",
          attr["triggered_entities"] == ["sensor.bat_living"])
    groups_renamed = [dict(g) for g in groups]
    groups_renamed[0][CONF_GROUP_NAME] = "客厅改名"
    entry.options = {CONF_BINDING_GROUPS: groups_renamed}
    coord2 = ConsumableTypeCoordinator(hass, entry, LABELS)
    check("重命名不漂移签名",
          coord2.entity_signature == (
              "replace_status:living", "replace_status:bedroom",
              "group_entity_data:living", "group_entity_data:bedroom"))

async def test_custom_entity_group(hass, monkeypatch) -> None:
    """自定义耗材实体分组：合成倒计时数据实体 + 按剩余时间阈值触发。"""
    now = datetime.now(timezone.utc)
    added_old = "2024-01-01"
    added_recent = (now - timedelta(days=30)).date().isoformat()
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {
                    CONF_GROUP_ID: "remote", CONF_GROUP_NAME: "马桶遥控器电池",
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: added_old,
                    CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
                    CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
                },
                {
                    CONF_GROUP_ID: "recent", CONF_GROUP_NAME: "刚换的",
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: added_recent,
                    CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
                    CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
                },
            ],
        },
    )
    inject_entries(monkeypatch, hass, [entry])
    coord = ConsumableTypeCoordinator(hass, entry, LABELS)
    groups = coord.groups
    check("custom groups 数量", len(groups) == 2)
    syn_remote = custom_consumable_entity_id("bat1", "remote")
    syn_recent = custom_consumable_entity_id("bat1", "recent")
    check("合成实体 id 确定",
          syn_remote == "sensor.consumable_manager_bat1_remote_custom")
    check("source_snapshots 含合成实体",
          {s["entity_id"] for s in coord.source_snapshots}
          == {syn_remote, syn_recent})
    check("custom entity_signature",
          coord.entity_signature == (
              "replace_status:remote", "replace_status:recent",
              "custom_consumable_data:remote", "custom_consumable_data:recent"))
    rem_old = coord.custom_remaining_in_unit(groups[0])
    rem_recent = coord.custom_remaining_in_unit(groups[1])
    check("逾期组剩余为负", rem_old is not None and rem_old < 0)
    check("近期组剩余为正", rem_recent is not None and rem_recent > 0)
    check("已使用时长为正数",
          coord.custom_elapsed_hours(groups[0]) is not None
          and coord.custom_elapsed_hours(groups[0]) > 0)
    hass.states.async_set(
        syn_remote, "-60.0", {"unit_of_measurement": "d"})
    hass.states.async_set(
        syn_recent, "150.0", {"unit_of_measurement": "d"})
    check("逾期组触发", coord.group_status(groups[0]) == STATE_REPLACE_NEEDED)
    check("未过期组正常", coord.group_status(groups[1]) == STATE_OK)
    attr = coord.group_attributes(groups[0])
    check("属性标记 custom_consumable_entity",
          attr.get("custom_consumable_entity") is True)
    check("属性含 added_at", attr.get("added_at") == added_old)
    check("属性含 remaining（负）",
          attr.get("remaining") is not None and attr["remaining"] < 0)
    check("属性含 lifespan", attr.get("lifespan") == 180)
    check("属性含 lifespan_unit", attr.get("lifespan_unit") == UNIT_DAYS)
    check("属性阈值类型 remaining_time",
          attr.get("threshold_type") == THRESHOLD_TYPE_REMAINING_TIME)
    check("属性阈值算子 less_than",
          attr.get("threshold_operator") == OPERATOR_LESS_THAN)
    check("属性绑定缺省为 None", attr.get("binding_consumable") is None)
    broken = {CONF_GROUP_ID: "x", CONF_GROUP_NAME: "缺时间",
              CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
              CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS}
    check("无 added_at 倒计时为 None",
          coord.custom_remaining_in_unit(broken) is None)
    check("无 added_at 不触发", coord.group_status(broken) == STATE_OK)

async def test_custom_entity_todo_and_binding(hass, monkeypatch) -> None:
    """自定义实体越阈 → 更换待办；绑定(Store)→ 待办描述含耗材型号。"""
    from consumable_manager.library import load_library
    clean_persist(hass)
    syn = custom_consumable_entity_id("batc", "remote")
    entry = make_entry(
        "batc", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {
                    CONF_GROUP_ID: "remote", CONF_GROUP_NAME: "马桶遥控器电池",
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: "2020-01-01",
                    CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
                    CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
                },
            ],
        },
    )
    library = load_library()
    coord = ConsumableTypeCoordinator(
        hass, entry, LABELS, library.type_meta("battery"), library)
    entry.runtime_data = ConsumableManagerData(coordinator=coord)
    inject_entries(monkeypatch, hass, [entry])
    await bindings.async_set_binding(hass, syn, "battery_cr2032")
    hass.states.async_set(syn, "-30.0", {"unit_of_measurement": "d"})
    await coord._async_refresh()
    uid = coord._auto_uid(TODO_KIND_REPLACE, syn)
    todos = [t for t in coord.todo_dicts() if t["uid"] == uid]
    check("自定义实体越阈生成更换待办", len(todos) == 1)
    check("待办描述含绑定耗材名",
          "CR2032" in (todos[0].get("description") or ""))

async def test_custom_entity_binding_reflection(hass, monkeypatch) -> None:
    """自定义实体绑定走 Store 层：group_attributes / 描述透出耗材，解绑回退未知。"""
    from consumable_manager.library import load_library
    clean_persist(hass)
    syn = custom_consumable_entity_id("batb", "remote")
    entry = make_entry(
        "batb", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {
                    CONF_GROUP_ID: "remote", CONF_GROUP_NAME: "马桶遥控器电池",
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: "2020-01-01",
                    CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
                    CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
                },
            ],
        },
    )
    library = load_library()
    coord = ConsumableTypeCoordinator(
        hass, entry, LABELS, library.type_meta("battery"), library)
    inject_entries(monkeypatch, hass, [entry])
    group = coord.groups[0]
    check("未绑定属性为 None",
          coord.group_attributes(group)["binding_consumable"] is None)
    check("未绑定描述回退未知",
          "未知" in (coord._replace_description(syn) or ""))
    await bindings.async_set_binding(hass, syn, "battery_cr2032")
    check("绑定后属性透出耗材名",
          "CR2032" in (coord.group_attributes(group)["binding_consumable"] or ""))
    check("绑定后描述透出耗材名",
          "CR2032" in (coord._replace_description(syn) or ""))
    await bindings.async_remove_binding(hass, syn)
    check("解绑后属性回退 None",
          coord.group_attributes(group)["binding_consumable"] is None)
    check("解绑后描述回退未知",
          "未知" in (coord._replace_description(syn) or ""))

async def test_custom_entity_mark_replaced_reset(hass, monkeypatch) -> None:
    """勾选更换：自定义实体 added_at 重置为 now → 倒计时重启、不再触发。"""
    from consumable_manager.library import load_library
    syn = custom_consumable_entity_id("batr", "remote")
    entry = make_entry(
        "batr", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {
                    CONF_GROUP_ID: "remote", CONF_GROUP_NAME: "马桶遥控器电池",
                    CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                    CONF_ADDED_AT: "2020-01-01",
                    CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_REMAINING_TIME,
                    CONF_THRESHOLD: 0, CONF_THRESHOLD_UNIT: UNIT_DAYS,
                    CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
                },
            ],
        },
    )
    library = load_library()
    coord = ConsumableTypeCoordinator(
        hass, entry, LABELS, library.type_meta("battery"), library)
    entry.runtime_data = ConsumableManagerData(coordinator=coord)
    inject_entries(monkeypatch, hass, [entry])
    hass.states.async_set(syn, "-30.0", {"unit_of_measurement": "d"})
    await coord._async_refresh()
    uid = coord._auto_uid(TODO_KIND_REPLACE, syn)
    check("越阈生成待办", any(t["uid"] == uid for t in coord.todo_dicts()))
    coord.async_mark_replaced(uid)
    reset_groups = coord.groups
    rg = next(g for g in reset_groups if g[CONF_GROUP_ID] == "remote")
    check("added_at 重置为今天",
          rg[CONF_ADDED_AT][:4] == str(date.today().year))
    new_remaining = coord.custom_remaining_in_unit(rg)
    check("重置后倒计时为正（重启）",
          new_remaining is not None and new_remaining > 0)
    hass.states.async_set(syn, str(new_remaining), {"unit_of_measurement": "d"})
    await coord._async_refresh()
    check("重置后整体不触发", coord.group_status(rg) == STATE_OK)

async def test_bindings_prime_avoids_blocking_read(hass, monkeypatch) -> None:
    """预热后同步读取不触碰磁盘。"""
    clean_persist(hass)
    await bindings.async_set_binding(hass, "sensor.x", "battery_cr2032")
    key = bindings._cache_key(hass)
    bindings._BINDINGS_CACHE.pop(key, None)
    bindings._PRIMED.discard(key)
    check("初始未预热", not bindings.is_primed(hass))
    calls: list[int] = []
    original = bindings._read_raw_sync
    def _counting(h) -> dict:
        calls.append(1)
        return original(h)
    monkeypatch.setattr(bindings, "_read_raw_sync", _counting)
    await bindings.async_prime(hass)
    check("预热完成", bindings.is_primed(hass))
    check("预热过程无同步读盘", not calls)
    check("预热后取到绑定",
          bindings.get_binding(hass, "sensor.x") == "battery_cr2032")
    check("预热后同步读取不再读盘", not calls)
    await bindings.async_prime(hass)
    check("重复预热不重复读盘", not calls)
    await bindings.async_set_binding(hass, "sensor.y", "battery_aa")
    check("写绑定不触发同步读盘", not calls)
    check("写入值可读回",
          bindings.get_binding(hass, "sensor.y") == "battery_aa")

async def test_type_entry_setup_creates_group_entities(hass, monkeypatch) -> None:
    """async_setup_entry：按分组逐组生成诊断实体 + 分组数据传感器。"""
    from consumable_manager import sensor as sensor_mod
    from consumable_manager.library import load_library
    library = load_library()
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {CONF_GROUP_ID: "g1", CONF_GROUP_NAME: "客厅",
                 CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.a"}]},
                {CONF_GROUP_ID: "g2", CONF_GROUP_NAME: "卧室",
                 CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.b"}]},
            ],
        },
    )
    coord = ConsumableTypeCoordinator(
        hass, entry, LABELS, library.type_meta("battery"), library)
    entry.runtime_data = ConsumableManagerData(coordinator=coord)
    entry2 = make_entry(
        "bat2", "电池2", {CONF_ENTRY_TYPE: "battery"},
        {CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.c"}]},
    )
    coord2 = ConsumableTypeCoordinator(hass, entry2, LABELS)
    entry2.runtime_data = ConsumableManagerData(coordinator=coord2)
    entry3 = make_entry(
        "bat3", "电池3", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {CONF_GROUP_ID: "cg1", CONF_GROUP_NAME: "遥控器电池",
                 CONF_GROUP_KIND: GROUP_KIND_CUSTOM,
                 CONF_ADDED_AT: "2026-08-01",
                 CONF_LIFESPAN: 180, CONF_LIFESPAN_UNIT: UNIT_DAYS},
            ],
        },
    )
    coord3 = ConsumableTypeCoordinator(hass, entry3, LABELS)
    entry3.runtime_data = ConsumableManagerData(coordinator=coord3)
    inject_entries(monkeypatch, hass, [entry, entry2, entry3])
    created: list = []
    await sensor_mod.async_setup_entry(
        hass, entry, lambda es: created.extend(es))
    replaces = [e for e in created if isinstance(e, ReplaceStatusSensor)]
    datas = [e for e in created if isinstance(e, GroupDataSensor)]
    check("每分组各生成 1 诊断 + 1 数据传感器（2 组→4 实体）",
          len(created) == 4 and len(replaces) == 2 and len(datas) == 2)
    check("诊断实体均为 ReplaceStatusSensor",
          all(isinstance(e, ReplaceStatusSensor) for e in replaces))
    uids = sorted(e._attr_unique_id for e in replaces)
    check("诊断实体 unique_id 含分组 id",
          uids == ["bat1_grp_g1", "bat1_grp_g2"])
    names = {e._attr_unique_id: e._attr_name for e in replaces}
    check("诊断实体名=分组名（避免与数据传感器冲突）",
          names["bat1_grp_g1"] == "客厅" and names["bat1_grp_g2"] == "卧室")
    duids = sorted(e._attr_unique_id for e in datas)
    check("数据传感器 unique_id 含 _grpdata_ 且区别于诊断",
          duids == ["bat1_grpdata_g1", "bat1_grpdata_g2"])
    check("数据传感器名 = 分组名 + 数据（避免与诊断实体重名）",
          datas[0]._attr_name == "客厅数据" and datas[1]._attr_name == "卧室数据")
    check("诊断实体图标继承类型图标",
          replaces[0].icon == "mdi:battery-outline")
    check("数据传感器图标继承类型图标",
          datas[0].icon == "mdi:battery-outline")
    data_g1 = datas[0]
    attrs = data_g1.extra_state_attributes
    check("数据传感器属性收敛为三项",
          set(attrs) == {"group", "consumable_type", "bound_entity_data"})
    check("已绑定实体数据为组装字符串（实体名 实体数据，无手动单位）",
          attrs["group"] == "客厅"
          and attrs["consumable_type"] == "battery"
          and attrs["bound_entity_data"] == ["sensor.a 未知"])
    check("数据传感器主状态为组最小值（无数据时为 None）",
          data_g1.native_value is None)
    hass.states.async_set(
        "sensor.a", "20", {"friendly_name": "书房温湿度传感器"})
    attrs2 = data_g1.extra_state_attributes
    check("实体名 + 实体原始返回值（无手动单位）",
          attrs2["bound_entity_data"] == ["书房温湿度传感器 20"])
    created2: list = []
    await sensor_mod.async_setup_entry(
        hass, entry2, lambda es: created2.extend(es))
    check("单组仍生成 诊断 + 数据 共 2 实体", len(created2) == 2)
    check("默认组 数据传感器 unique_id",
          [e._attr_unique_id for e in created2
           if isinstance(e, GroupDataSensor)] == ["bat2_grpdata_default"])
    created3: list = []
    await sensor_mod.async_setup_entry(
        hass, entry3, lambda es: created3.extend(es))
    replaces3 = [e for e in created3 if isinstance(e, ReplaceStatusSensor)]
    customs3 = [e for e in created3 if isinstance(e, CustomConsumableSensor)]
    check("自定义分组生成 诊断 + 倒计时数据 共 2 实体", len(created3) == 2)
    check("自定义分组含诊断实体", len(replaces3) == 1)
    check("自定义分组含倒计时数据传感器", len(customs3) == 1)
    ccs = customs3[0]
    check("倒计时实体 forced entity_id 确定性",
          ccs._attr_entity_id == custom_consumable_entity_id("bat3", "cg1"))
    check("倒计时实体名=分组名+数据", ccs._attr_name == "遥控器电池数据")
    check("倒计时实体单位=d", ccs.native_unit_of_measurement == "d")
    check("倒计时实体设备类=DURATION", ccs.device_class == "duration")
    check("倒计时实体倒计时为正（按寿命）",
          ccs.native_value is not None and ccs.native_value > 0)

async def test_group_regex_dynamic(hass, monkeypatch) -> None:
    """正则规则动态匹配：手动多选 ∪ 正则 live，新增匹配实体自动入组。"""
    import homeassistant.helpers.entity_registry as er_mod
    class _E:
        disabled_by = None
    class _EReg:
        entities = {
            "sensor.bat_a": _E(),
            "sensor.bat_b": _E(),
            "sensor.bat_c": _E(),
            "sensor.light_x": _E(),  # 不匹配正则（非 bat_ 前缀）
        }
        def async_get(self, key):
            return None
    monkeypatch.setattr(er_mod, "async_get", lambda h: _EReg())
    entry = make_entry(
        "bat_regex", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [{
                CONF_GROUP_ID: "g1",
                CONF_GROUP_NAME: "全屋电池",
                CONF_GROUP_KIND: GROUP_KIND_BINDING,
                CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.bat_a"}],
                CONF_ENTITY_REGEX: r"^sensor\.bat_",
            }],
        },
    )
    inject_entries(monkeypatch, hass, [entry])
    coord = ConsumableTypeCoordinator(hass, entry, LABELS)
    group = coord.groups[0]
    live = coord._group_live_entities(group)
    check("手动∪正则 live 成员",
          set(live) == {"sensor.bat_a", "sensor.bat_b", "sensor.bat_c"})
    check("手动固定仅显式实体",
          coord._group_manual_entities(group) == ["sensor.bat_a"])
    check("正则命中全部匹配实体",
          set(coord._regex_match_entities(r"^sensor\.bat_"))
          == {"sensor.bat_a", "sensor.bat_b", "sensor.bat_c"})
    resolved = coord._group_resolved(group)
    check("解析实体含正则动态成员",
          set(resolved) >= {"sensor.bat_b", "sensor.bat_c"})
    snap_ids = sorted(s["entity_id"] for s in coord.source_snapshots)
    check("快照含正则动态成员",
          snap_ids == ["sensor.bat_a", "sensor.bat_b", "sensor.bat_c"])
    _EReg.entities["sensor.bat_d"] = _E()
    check("新增实体自动入组",
          "sensor.bat_d" in coord._group_live_entities(group))
    check("动态快照含新实体",
          "sensor.bat_d" in
          [s["entity_id"] for s in coord.source_snapshots])
    no_regex = {
        CONF_GROUP_ID: "g2", CONF_GROUP_NAME: "仅手动",
        CONF_GROUP_KIND: GROUP_KIND_BINDING,
        CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.x"}],
    }
    check("无正则仅手动固定",
          coord._group_live_entities(no_regex) == ["sensor.x"])
