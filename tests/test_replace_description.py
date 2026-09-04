"""更换待办描述（结构化 / markdown / 无设备回退）（真环境 pytest-ha）。"""

from __future__ import annotations
from types import SimpleNamespace
from homeassistant.helpers import area_registry as ar_mod
from homeassistant.helpers import device_registry as dr_mod
from homeassistant.helpers import entity_registry as er_mod
from consumable_manager.coordinator import ConsumableTypeCoordinator
from consumable_manager.const import (
    CONF_ENTRY_TYPE,
    CONF_SOURCE_ENTITIES,
    CONF_THRESHOLD,
    CONF_THRESHOLD_TYPE,
    CONF_THRESHOLD_UNIT,
    THRESHOLD_TYPE_LIFETIME_PERCENT,

)

def check(desc: str, cond: bool) -> None:
    """离线 harness 的 check(desc, cond) 断言 → pytest assert。"""
    assert cond, desc

def make_entry(entry_id: str, title: str, data: dict, options: dict | None = None):
    """协调器测试用最小 ConfigEntry 替身（真实 DataUpdateCoordinator 仅存引用）。"""
    entry = SimpleNamespace(
        entry_id=entry_id,
        title=title,
        data=dict(data),
        options=dict(options or {}),
        runtime_data=None,
    )
    unloads: list = []
    entry.async_on_unload = unloads.append  # type: ignore[attr-defined]
    return entry
LABELS = {
    "replace": "更换", "purchase": "购买",
    "low_stock": "库存告急，请购买。", "replace_needed": "请更换耗材。",
    "last_replaced": "上次更换", "consumables": "耗材",
    "desc_area": "区域", "desc_device": "设备", "desc_entity": "实体",
    "desc_specs": "规格", "desc_threshold": "阈值", "unknown": "未知",
}

# ---- registry 注入替身（预设设备/实体/区域关系）----
class _Ent:
    def __init__(self, device_id):
        self.device_id = device_id

class _Dev:
    def __init__(self, area_id, manufacturer, model, name):
        self.area_id = area_id
        self.manufacturer = manufacturer
        self.model = model
        self.name = name

class _Area:
    def __init__(self, name):
        self.name = name

class _AreaReg:
    def async_get_area(self, area_id):
        return _Area("书房") if area_id == "area_study" else None

class _DevReg:
    def async_get(self, device_id):
        if device_id == "dev_purifier":
            return _Dev("area_study", "Xiaomi",
                        "zhimi.airpurifier.m1", "空气净化器")
        return None

class _EntReg:
    def async_get(self, entity_id):
        if entity_id == "sensor.purifier":
            return _Ent("dev_purifier")
        return None

class _EntNoDev:
    device_id = None

class _EntRegNoDev:
    def async_get(self, entity_id):
        if entity_id == "sensor.kitty_rule":
            return _EntNoDev()
        return None

def _regs_purifier(monkeypatch) -> None:
    monkeypatch.setattr(ar_mod, "async_get", lambda hass: _AreaReg())
    monkeypatch.setattr(dr_mod, "async_get", lambda hass: _DevReg())
    monkeypatch.setattr(er_mod, "async_get", lambda hass: _EntReg())

# ---- 用例 ----
async def test_replace_description_structure(hass, monkeypatch) -> None:
    """更换待办描述结构化：区域/设备/实体/耗材/规格；未绑定耗材显示未知。"""
    _regs_purifier(monkeypatch)
    from consumable_manager.library import load_library
    library = load_library()
    from consumable_manager import bindings as _bindings
    entry = make_entry(
        "pur1", "空气净化器", {CONF_ENTRY_TYPE: "filter"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.purifier",
                "device_name": "空气净化器",
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    await _bindings.async_set_binding(
        hass, "sensor.purifier", "filter_xiaomi_airpurifier_2")
    hass.config.language = "zh-Hans"  # 描述分隔符/标签按中文
    coord = ConsumableTypeCoordinator(hass, entry, LABELS, None, library)
    hass.states.async_set("sensor.purifier", "10.0")
    desc = coord._replace_description("sensor.purifier") or ""
    check("描述含区域行", "**区域**：书房" in desc)
    check("描述含设备行", "**设备**：空气净化器" in desc)
    check("描述含实体行", "**实体**：sensor.purifier" in desc)
    check("描述含耗材行", "**耗材**：" in desc)
    check("描述含规格行", "**规格**：" in desc and "compatible" in desc)
    entry2 = make_entry(
        "bat2", "电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.generic", "device_name": "通用设备",
                "device_model": "no-such-model", "manufacturer": "NoSuch",
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    coord2 = ConsumableTypeCoordinator(hass, entry2, LABELS, None, library)
    hass.states.async_set("sensor.generic", "5.0")
    desc2 = coord2._replace_description() or ""
    check("未绑定耗材显示未知", "**耗材**：未知" in desc2)
    check("无规格行", "**规格**：" not in desc2)

async def test_replace_description_markdown_format(hass, monkeypatch) -> None:
    """更换待办描述应为 Markdown：标签加粗；规格用行内代码且省略名称前缀。"""
    _regs_purifier(monkeypatch)
    from consumable_manager.library import load_library
    library = load_library()
    entry = make_entry(
        "pur_md", "空气净化器", {CONF_ENTRY_TYPE: "filter"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.purifier",
                "device_name": "空气净化器",
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    from consumable_manager import bindings as _bindings
    await _bindings.async_set_binding(
        hass, "sensor.purifier", "filter_xiaomi_airpurifier_2")
    hass.config.language = "zh-Hans"
    coord = ConsumableTypeCoordinator(hass, entry, LABELS, None, library)
    hass.states.async_set("sensor.purifier", "10.0")
    desc = coord._replace_description() or ""
    check("区域标签加粗", "**区域**：" in desc)
    check("设备标签加粗", "**设备**：" in desc)
    check("实体标签加粗", "**实体**：" in desc)
    check("耗材标签加粗", "**耗材**：" in desc)
    check("规格标签加粗", "**规格**：" in desc)
    idx = desc.find("**规格**：")
    spec_part = desc[idx + len("**规格**："):] if idx >= 0 else ""
    check("规格紧随行内代码", spec_part.startswith("`"))
    check("规格含 meta 字段", "compatible" in desc)
    check("规格无名称前缀", not spec_part.startswith("`") or spec_part[1:].lstrip().startswith("{"))

async def test_replace_description_no_device_unknown(hass, monkeypatch) -> None:
    """实体无设备关联（取不到 manufacturer/model）时，显示「未知」，不列类型全部耗材。"""
    monkeypatch.setattr(er_mod, "async_get", lambda hass: _EntRegNoDev())
    from consumable_manager.library import load_library
    library = load_library()  # 内置库 battery 类型含纽扣电池等耗材
    entry = make_entry(
        "b_kitty", "哈基咪电池", {CONF_ENTRY_TYPE: "battery"},
        {
            CONF_SOURCE_ENTITIES: [{
                "entity_id": "sensor.kitty_rule",
                "device_name": None,
                "device_model": None,
                "manufacturer": None,
            }],
            CONF_THRESHOLD_TYPE: THRESHOLD_TYPE_LIFETIME_PERCENT,
            CONF_THRESHOLD: 20,
            CONF_THRESHOLD_UNIT: "%",
        },
    )
    hass.config.language = "zh-Hans"
    coord = ConsumableTypeCoordinator(hass, entry, LABELS, None, library)
    hass.states.async_set("sensor.kitty_rule", "10.0")
    desc = coord._replace_description() or ""
    check("无设备实体未匹配→显示未知（不再列全部）",
          "**耗材**：未知" in desc)
