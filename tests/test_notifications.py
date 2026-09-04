"""通知（触发 / 边沿 / 定时 / 配置界面 / 基线）（真环境 pytest-ha）。"""

from __future__ import annotations
import homeassistant.helpers.translation as _translation_mod
from _helpers import (
    LABELS,
    check,
    inject_entries,
    install_service_spy,
    make_call,
    make_entry,

)

from consumable_manager.coordinator import (
    TODO_STATUS_NEEDS_ACTION,
    ConsumableManagerData,
    ConsumableTypeCoordinator,
    StockCoordinator,

)

from consumable_manager.const import (
    CONF_ENTRY_TYPE,
    CONF_ITEM_ID,
    CONF_LAST_TRIGGERED_SIG,
    CONF_ITEM_NAME,
    CONF_ITEM_TYPE,
    CONF_NOTIFICATION,
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
    CONF_GROUP_ID,
    CONF_GROUP_NAME,
    CONF_STOCK_ITEMS,
    CONF_STOCK_THRESHOLD,
    CONF_THRESHOLD,
    CONF_THRESHOLD_OPERATOR,
    CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT,
    CONF_UNIT,
    ENTRY_TYPE_NOTIFICATION,
    ENTRY_TYPE_STOCK,
    OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_LIFETIME_PERCENT,
    UNIT_PERCENT,

)

from consumable_manager.notifications import (
    async_flush_entry,
    async_scheduled_flush,
    find_notification_config,
    normalize_notification_section,

)

from consumable_manager import services as svc

def _stub_translations(monkeypatch) -> None:
    """翻译读取返回空 → _integration_title 回退（不依赖组件翻译文件存在）。"""
    async def _empty(hass, language, category, **kwargs):
        return {}
    monkeypatch.setattr(
        _translation_mod, "async_get_translations", _empty)

# ---- 用例 ----
async def test_notifications(hass, monkeypatch) -> None:
    """通知：条目级优先/全局兜底；实时跳变单发；样式覆盖；持续异常不重复。"""
    item = {
        CONF_ITEM_ID: "a", CONF_ITEM_NAME: "滤芯", CONF_ITEM_TYPE: "filter",
        CONF_QUANTITY: 5, CONF_UNIT: "个", CONF_STOCK_THRESHOLD: 2,
    }
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: ["notify.phone"],
        }},
    )
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [item]},
    )
    check("非 dict 段视为未配置",
          normalize_notification_section(None) is None)
    check("空渠道段视为未配置",
          normalize_notification_section(
              {CONF_NOTIFY_SYSTEM: False, CONF_NOTIFY_ENTITIES: []}) is None)
    norm = normalize_notification_section({
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: [],
    })
    check("mode 缺省实时",
          norm[CONF_NOTIFY_MODE] == NOTIFY_MODE_REALTIME)
    check("style 缺省人性化", norm[CONF_NOTIFY_STYLE] == NOTIFY_STYLE_HUMAN)
    check("无配置不发", find_notification_config(hass, {}) is None)
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, stock_entry])
    global_conf = find_notification_config(hass, {})
    check("全局兜底生效",
          global_conf is not None and global_conf[CONF_NOTIFY_SYSTEM] is True)
    override_conf = find_notification_config(hass, {
        CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: False,
            CONF_NOTIFY_ENTITIES: ["notify.x"],
            CONF_NOTIFY_STYLE: NOTIFY_STYLE_VALUE,
        },
    })
    check("条目级覆盖优先",
          override_conf is not None
          and override_conf[CONF_NOTIFY_ENTITIES] == ["notify.x"]
          and override_conf[CONF_NOTIFY_SYSTEM] is False
          and override_conf[CONF_NOTIFY_STYLE] == NOTIFY_STYLE_VALUE
          and override_conf[CONF_NOTIFY_MODE] == NOTIFY_MODE_REALTIME)
    coord = StockCoordinator(hass, stock_entry, LABELS)
    await coord._async_refresh()
    check("正常基线不发通知", len(calls) == 0)
    coord.async_set_quantity("a", 0)
    await coord._async_refresh()
    calls_now = list(calls)
    check("跳变发送两条渠道", len(calls_now) == 2)
    check("系统通知渠道正确",
          calls_now[0]["domain"] == "persistent_notification"
          and calls_now[0]["service"] == "create")
    check("通知实体渠道正确",
          calls_now[1]["domain"] == "notify"
          and calls_now[1]["service"] == "send_message"
          and calls_now[1]["target"] == {"entity_id": ["notify.phone"]})
    check("实时标题为条目名", calls_now[0]["data"]["title"] == "库存")
    check("人性化文案", calls_now[0]["data"]["message"]
          == "滤芯 库存告急，请购买。")
    check("notification_id 稳定",
          calls_now[0]["data"]["notification_id"]
          == "consumable_manager_stk1")
    await coord._async_refresh()
    check("持续异常不重复发送", len(calls) == 2)
    coord.async_set_quantity("a", 5)
    await coord._async_refresh()
    check("恢复正常不发通知", len(calls) == 2)
    coord.async_set_quantity("a", 0)
    await coord._async_refresh()
    check("恢复后再触发再发", len(calls) == 4)
    stock_entry.options[CONF_NOTIFICATION] = {
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: [],
        CONF_NOTIFY_STYLE: NOTIFY_STYLE_VALUE,
    }
    calls.clear()
    coord.async_set_quantity("a", 5)
    await coord._async_refresh()
    coord.async_set_quantity("a", 0)
    await coord._async_refresh()
    last = calls[-1]
    check("状态值样式", last["data"]["message"] == "滤芯 0个")

async def test_first_refresh_abnormal_silent(hass, monkeypatch) -> None:
    """首次刷新即异常（重启后仍低库存）：基线未建立，不发。"""
    item = {
        CONF_ITEM_ID: "a", CONF_ITEM_NAME: "滤芯", CONF_ITEM_TYPE: "filter",
        CONF_QUANTITY: 0, CONF_UNIT: "个", CONF_STOCK_THRESHOLD: 2,
    }
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: [],
        }},
    )
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [item]},
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, stock_entry])
    coord = StockCoordinator(hass, stock_entry, LABELS)
    await coord._async_refresh()
    check("首次异常不发（基线未建立）", len(calls) == 0)

async def test_bind_no_spurious_notification(hass, monkeypatch) -> None:
    """绑定已越阈实体：动作本身纯映射（不刷新→无待办/无通知）；"""
    filter_entry = make_entry(
        "flt1", "滤芯", {CONF_ENTRY_TYPE: "filter"},
        {
            CONF_BINDING_GROUPS: [{
                CONF_GROUP_ID: "default",
                CONF_GROUP_NAME: "滤芯",
                CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.filter_life"}],
            }],
            CONF_NOTIFICATION: {
                CONF_NOTIFY_SYSTEM: True,
                CONF_NOTIFY_ENTITIES: ["notify.phone"],
                CONF_NOTIFY_STYLE: NOTIFY_STYLE_HUMAN,
            },
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: UNIT_PERCENT,
        },
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [filter_entry])
    filter_coord = ConsumableTypeCoordinator(hass, filter_entry, LABELS)
    filter_entry.runtime_data = ConsumableManagerData(coordinator=filter_coord)
    hass.states.async_set("sensor.filter_life", "10.0")
    calls_before = len(calls)
    res = await svc.async_bind_entity(hass, make_call(hass, "bind_entity", {
        "entity_id": "sensor.filter_life",
        "consumable_id": "filter_hepa13",
    }))
    check("绑定成功", res["entry_type"] == "filter")
    check("绑定动作本身不发通知",
          len(calls) == calls_before)
    check("绑定动作本身不生成待办（纯映射）",
          len(filter_coord.todo_dicts()) == 0)
    await filter_coord._async_refresh()
    triggered_todos = [
        t for t in filter_coord.todo_dicts()
        if t["status"] == TODO_STATUS_NEEDS_ACTION
        and "sensor.filter_life" in t["uid"]
    ]
    check("重载后越阈实体仍建待办（监控职责）", len(triggered_todos) == 1)
    check("重载过程仍不发通知",
          len(calls) == calls_before)
    hass.states.async_set("sensor.filter_life", "90.0")
    await filter_coord._async_refresh()
    calls_after_normal = len(calls)
    check("恢复正常的绑定期间无残留通知",
          calls_after_normal == calls_before)
    hass.states.async_set("sensor.filter_life", "10.0")
    await filter_coord._async_refresh()
    check("真实跳变仍发通知",
          len(calls) > calls_after_normal)

async def test_sync_alert_baseline_force_persist(hass, monkeypatch) -> None:
    """配置变更（绑定/解绑）期间即便基线尚未建立，sync_alert_baseline 也必须"""
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_BINDING_GROUPS: [
                {
                    CONF_GROUP_ID: "living", CONF_GROUP_NAME: "客厅",
                    CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.bat_living"}],
                },
            ],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    inject_entries(monkeypatch, hass, [entry])
    coord = ConsumableTypeCoordinator(hass, entry, LABELS)
    coord._baseline_established = False
    coord.sync_alert_baseline()
    check("基线未建立时仍 force 持久化签名",
          entry.options.get(CONF_LAST_TRIGGERED_SIG) == "0:")

async def test_notifications_type_entry(hass, monkeypatch) -> None:
    """类型条目：越阈值实时跳变 + 两种消息样式（人性化 / 状态值）。"""
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: [],
        }},
    )
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.x_battery",
                "device_name": "书房温湿度传感器",
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, type_entry])
    hass.config.language = "zh-Hans"
    coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    hass.states.async_set("sensor.x_battery", "80.0")
    await coord._async_refresh()
    check("类型条目正常基线不发", len(calls) == 0)
    hass.states.async_set("sensor.x_battery", "15.0")
    await coord._async_refresh()
    calls_now = list(calls)
    check("类型条目跳变发送", len(calls_now) == 1
          and calls_now[0]["domain"] == "persistent_notification")
    check("实时标题为类型名", calls_now[0]["data"]["title"] == "电池")
    check("人性化文案用设备名",
          calls_now[0]["data"]["message"]
          == "书房温湿度传感器 请更换耗材。")
    notify_entry.options[CONF_NOTIFICATION][CONF_NOTIFY_STYLE] = (
        NOTIFY_STYLE_VALUE
    )
    calls.clear()
    hass.states.async_set("sensor.x_battery", "80.0")
    await coord._async_refresh()
    hass.states.async_set("sensor.x_battery", "15.0")
    await coord._async_refresh()
    last = calls[-1]
    check("状态值样式含设备名与值",
          last["data"]["message"] == "书房温湿度传感器 15%")

async def test_notifications_reload_baseline(hass, monkeypatch) -> None:
    """reload（重建协调器）不吞跳变：运行期正常过的条目，改值异常仍应发送。"""
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: [],
        }},
    )
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.x_battery",
                "device_name": "书房温湿度传感器",
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, type_entry])
    hass.config.language = "zh-Hans"
    coord1 = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    hass.states.async_set("sensor.x_battery", "80.0")
    await coord1._async_refresh()  # 首次（baseline 未建立）：不发
    check("reload 测试 setup 首次不发", len(calls) == 0)
    await coord1._async_refresh()  # 运行期刷新：持久化 ok（空集合签名 "0:"）
    check("运行期持久化正常基线（空集合签名）",
          type_entry.options.get(CONF_LAST_TRIGGERED_SIG) == "0:")
    coord2 = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    hass.states.async_set("sensor.x_battery", "15.0")
    await coord2._async_refresh()
    calls_now = list(calls)
    check("reload 后异常跳变仍发送", len(calls_now) == 1
          and calls_now[0]["domain"] == "persistent_notification")

async def test_fresh_entry_first_abnormal_no_notify(hass, monkeypatch) -> None:
    """全新建条目（无持久化基线）首次异常仍不补发（设计保持）。"""
    notify_entry = make_entry(
        "ntf2", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: [],
        }},
    )
    type_entry = make_entry(
        "bat2", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.y_battery"}],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, type_entry])
    coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    hass.states.async_set("sensor.y_battery", "15.0")
    await coord._async_refresh()
    check("全新条目首次异常不补发", len(calls) == 0)

async def test_threshold_change_multi_trigger(hass, monkeypatch) -> None:
    """改阈值使多实体同时越界：待办逐条生成 + 通知发送（reload 路径）。"""
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: [],
        }},
    )
    entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [
                {"entity_id": "sensor.a", "device_name": "A 设备"},
                {"entity_id": "sensor.b", "device_name": "B 设备"},
                {"entity_id": "sensor.c", "device_name": "C 设备"},
            ],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_OPERATOR: OPERATOR_LESS_THAN,
            CONF_THRESHOLD_UNIT: "%",
            CONF_NOTIFICATION: {
                CONF_NOTIFY_SYSTEM: True, CONF_NOTIFY_ENTITIES: [],
            },
        },
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, entry])
    hass.config.language = "zh-Hans"
    coord1 = ConsumableTypeCoordinator(hass, entry, LABELS)
    hass.states.async_set("sensor.a", "80.0")
    hass.states.async_set("sensor.b", "70.0")
    hass.states.async_set("sensor.c", "60.0")
    await coord1._async_refresh()   # 首次（baseline 未建立）：不发
    await coord1._async_refresh()   # 运行期：持久化 ok（空集合签名 "0:"）
    check("多实体运行期持久化 ok 基线（空集合签名）",
          entry.options.get(CONF_LAST_TRIGGERED_SIG) == "0:")
    check("阈值 20 时无待办",
          len([t for t in coord1.todo_dicts()
               if "_replace_" in t["uid"]]) == 0)
    entry.options[CONF_THRESHOLD] = 50
    hass.states.async_set("sensor.a", "30.0")
    hass.states.async_set("sensor.b", "40.0")
    coord2 = ConsumableTypeCoordinator(hass, entry, LABELS)
    await coord2._async_refresh()
    todos = coord2.todo_dicts()
    replace = [t for t in todos if "_replace_" in t["uid"]]
    check("改阈值后两个越界实体各一条待办", len(replace) == 2)
    summaries = {t["summary"] for t in replace}
    check("越界实体待办标题正确",
          "A 设备 请更换耗材。" in summaries
          and "B 设备 请更换耗材。" in summaries)
    calls_now = list(calls)
    check("改阈值后跳变发送通知", len(calls_now) == 1
          and calls_now[0]["domain"] == "persistent_notification")

async def test_notifications_scheduled(hass, monkeypatch) -> None:
    """定时统一推送：跳变只标记不发送；到点合并一条（标题=集成名）。"""
    _stub_translations(monkeypatch)
    item = {
        CONF_ITEM_ID: "a", CONF_ITEM_NAME: "滤芯", CONF_ITEM_TYPE: "filter",
        CONF_QUANTITY: 5, CONF_UNIT: "个", CONF_STOCK_THRESHOLD: 1,
    }
    stock_entry = make_entry(
        "stk1", "库存", {CONF_ENTRY_TYPE: ENTRY_TYPE_STOCK},
        {CONF_STOCK_ITEMS: [item]},
    )
    type_entry = make_entry(
        "bat1", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{"entity_id": "sensor.x_battery"}],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
        },
    )
    notify_entry = make_entry(
        "ntf1", "通知设置", {CONF_ENTRY_TYPE: ENTRY_TYPE_NOTIFICATION},
        {CONF_NOTIFICATION: {
            CONF_NOTIFY_SYSTEM: True,
            CONF_NOTIFY_ENTITIES: ["notify.a", "notify.b"],
            CONF_NOTIFY_MODE: NOTIFY_MODE_SCHEDULED,
            CONF_NOTIFY_SCHEDULE_TIME: "20:00",
        }},
    )
    calls = install_service_spy(monkeypatch, hass)
    inject_entries(monkeypatch, hass, [notify_entry, stock_entry, type_entry])
    hass.config.language = "zh-Hans"
    stock_coord = StockCoordinator(hass, stock_entry, LABELS)
    stock_entry.runtime_data = ConsumableManagerData(coordinator=stock_coord)
    type_coord = ConsumableTypeCoordinator(hass, type_entry, LABELS)
    type_entry.runtime_data = ConsumableManagerData(coordinator=type_coord)
    hass.states.async_set("sensor.x_battery", "80.0")
    await stock_coord._async_refresh()
    await type_coord._async_refresh()  # 基线正常
    hass.states.async_set("sensor.x_battery", "10.0")
    await type_coord._async_refresh()
    stock_coord.async_set_quantity("a", 0)
    await stock_coord._async_refresh()
    check("定时模式跳变不发", len(calls) == 0)
    check("待推送标记置位",
          type_coord.alert_pending is True
          and stock_coord.alert_pending is True)
    await async_scheduled_flush(hass)
    calls_now = list(calls)
    check("统一推送两条渠道", len(calls_now) == 2
          and calls_now[0]["domain"] == "persistent_notification"
          and calls_now[1]["domain"] == "notify")
    check("统一推送 id 固定", calls_now[0]["data"]["notification_id"]
          == "consumable_manager_scheduled")
    check("统一标题为集成名",
          calls_now[0]["data"]["title"] == "库存")  # 翻译空 → 回退首条标题
    msg = calls_now[0]["data"]["message"]
    check("统一消息逐行带条目名",
          "库存：滤芯 库存告急，请购买。" in msg
          and "电池：" in msg and "请更换耗材。" in msg)
    check("统一推送实体取并集",
          calls_now[1]["target"] == {"entity_id": ["notify.a", "notify.b"]})
    await async_scheduled_flush(hass)
    check("推送后不重复", len(calls) == 2)
    check("待推送标记清除",
          type_coord.alert_pending is False
          and stock_coord.alert_pending is False)
    calls.clear()
    hass.states.async_set("sensor.x_battery", "90.0")
    await type_coord._async_refresh()
    stock_coord.async_set_quantity("a", 5)
    await stock_coord._async_refresh()
    check("恢复清除待推送标记",
          type_coord.alert_pending is False
          and stock_coord.alert_pending is False)
    await async_scheduled_flush(hass)
    check("恢复后统一推送无内容", len(calls) == 0)
    type_entry.options[CONF_NOTIFICATION] = {
        CONF_NOTIFY_SYSTEM: True,
        CONF_NOTIFY_ENTITIES: [],
        CONF_NOTIFY_MODE: NOTIFY_MODE_SCHEDULED,
        CONF_NOTIFY_SCHEDULE_TIME: "21:30",
    }
    calls.clear()
    hass.states.async_set("sensor.x_battery", "10.0")
    await type_coord._async_refresh()
    check("条目级定时跳变不发", len(calls) == 0
          and type_coord.alert_pending is True)
    await async_scheduled_flush(hass)
    check("条目级定时不参与全局合并", len(calls) == 0)
    await async_flush_entry(hass, type_entry)
    calls_now = list(calls)
    check("条目级独立推送一条", len(calls_now) == 1
          and calls_now[0]["data"]["notification_id"]
          == "consumable_manager_bat1")
    check("条目级标题为耗材类型", calls_now[0]["data"]["title"] == "电池")
    check("条目级推送后清标记", type_coord.alert_pending is False)
