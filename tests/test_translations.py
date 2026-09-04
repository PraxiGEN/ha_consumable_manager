"""翻译文件完整性 / services.yaml 字段翻译（真环境 pytest-ha）。"""

from __future__ import annotations
import json
import re
from pathlib import Path

from _helpers import INTEGRATION, REPO_ROOT

def check(desc: str, cond: bool) -> None:
    """离线 harness 的 check(desc, cond) 断言 → pytest assert。"""
    assert cond, desc

# ---- 用例 ----
def test_translations() -> None:
    """翻译文件必须包含实体属性的翻译键（属性名 + 枚举属性值）。"""
    base = INTEGRATION / "translations"
    for lang in ("zh-Hans", "en"):
        data = json.loads((base / f"{lang}.json").read_text(encoding="utf-8"))
        check(f"{lang} 顶层 entity 结构",
              "entity" in data and "sensor" in data["entity"]
              and "todo" in data["entity"])
        sensor = data["entity"]["sensor"]
        check(f"{lang} 状态翻译",
              sensor["stock_status"]["state"]["ok"]
              and sensor["replace_status"]["state"]["replace_needed"])
        required_names = {
            "stock_status": {"item_count", "low_items"},
            "replace_status": {
                "consumable_type", "group", "custom_consumable_entity", "threshold_type", "threshold",
                "threshold_unit", "threshold_operator",
                "source_entities", "manual_entities", "regex_matched",
                "triggered_entities", "last_replaced", "added_at", "elapsed",
            },
            "group_entity_data": {
                "group", "consumable_type", "bound_entity_data",
            },
            "stock_item": {
                "item_id", "consumable_type", "unit",
                "stock_threshold", "low_stock",
            },
        }
        for key, attrs in required_names.items():
            got = set(sensor[key]["state_attributes"])
            check(f"{lang} {key} 属性名翻译齐全", got >= attrs)
        state_attrs = sensor["replace_status"]["state_attributes"]
        check(f"{lang} 阈值类型值翻译",
              state_attrs["threshold_type"]["state"]["lifetime_percent"])
        check(f"{lang} 计算方式值翻译",
              state_attrs["threshold_operator"]["state"]["less_than"])
        check(f"{lang} 单位值翻译",
              state_attrs["threshold_unit"]["state"]["hours"])
        check(f"{lang} 耗材类型值翻译",
              state_attrs["consumable_type"]["state"]["battery"])
        check(f"{lang} 布尔属性值翻译",
              sensor["stock_item"]["state_attributes"]["low_stock"]["state"]["true"])
        services = data["services"]
        check(f"{lang} add_type 服务翻译",
              "add_type" in services
              and "type_key" in services["add_type"]["fields"]
              and "default_threshold_type"
              in services["add_type"]["fields"]
              and "default_threshold_unit"
              in services["add_type"]["fields"])
        check(f"{lang} add_consumable 无 id 字段",
              "id" not in services["add_consumable"]["fields"]
              and set(services["add_consumable"]["fields"]) == {
                  "cons_type", "model", "name", "unit", "meta"})
        check(f"{lang} 通知类型选项",
              data["selector"]["entry_type"]["options"]["notification"])
        opt_steps = data["options"]["step"]
        check(f"{lang} 菜单含通知项",
              opt_steps["init"]["menu_options"]["notification"])
        check(f"{lang} 通知表单字段齐全",
              {"customize", "system", "entities", "style", "mode",
               "schedule_time"}
              <= set(opt_steps["notification"]["data"]))
        check(f"{lang} 模式选项翻译齐全",
              {"realtime", "scheduled"}
              <= set(data["selector"]["notify_mode"]["options"]))
        check(f"{lang} 样式选项翻译齐全",
              {"human", "value"}
              <= set(data["selector"]["notify_style"]["options"]))
        check(f"{lang} 通知渠道错误翻译",
              data["options"]["error"]["no_channel"])
        notify_text = data["selector"]["notify_text"]["options"]
        check(f"{lang} 固定话术翻译齐全",
              {"low_stock", "replace_needed", "last_replaced", "consumables"}
              <= set(notify_text))
        for name, body in data.get("selector", {}).items():
            check(f"{lang} selector.{name} 仅含 options 子键",
                  set(body) == {"options"})
            for key in body["options"]:
                check(f"{lang} selector.{name}.{key} 键名合法",
                      re.fullmatch(r"[a-z0-9-_]+", key) is not None
                      and not key.startswith(("-", "_"))
                      and not key.endswith(("-", "_")))
        for section in ("config", "options"):
            for step, info in (
                data.get(section, {}).get("step", {}).items()
            ):
                check(f"{lang} {section}.step.{step} 无非法子键",
                      set(info) <= {"title", "description", "data",
                                    "data_description", "menu_options",
                                    "submit", "sections"})
        def _no_edge_ws(node, prefix="") -> bool:
            for k, v in node.items():
                p = f"{prefix}.{k}" if prefix else k
                if isinstance(v, str) and v != v.strip():
                    print(f"  违规值: {p} = {v!r}")
                    return False
                if isinstance(v, dict) and not _no_edge_ws(v, p):
                    return False
            return True
        check(f"{lang} 翻译值无首尾空白", _no_edge_ws(data))

def test_services_yaml_fields_translated() -> None:
    """services.yaml 每个服务的字段都必须在翻译文件中有对应翻译键（防漏翻）。"""
    svc_path = INTEGRATION / "services.yaml"
    lines = svc_path.read_text(encoding="utf-8").splitlines()
    yaml_fields: dict[str, set[str]] = {}
    cur = None
    in_fields = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]\w*):", raw)
        if m and not raw[0].isspace():
            cur = m.group(1)
            in_fields = False
            yaml_fields.setdefault(cur, set())
            continue
        if cur is None:
            continue
        if re.match(r"^  fields:\s*$", raw):
            in_fields = True
            continue
        if in_fields and re.match(r"^  [A-Za-z_]\w*:", raw):
            in_fields = False
            continue
        if in_fields:
            fm = re.match(r"^    ([A-Za-z_]\w*):", raw)
            if fm:
                yaml_fields[cur].add(fm.group(1))
    for lang in ("zh-Hans", "en"):
        tpath = INTEGRATION / "translations" / f"{lang}.json"
        tdata = json.loads(tpath.read_text(encoding="utf-8"))
        svc_trans = tdata.get("services", {})
        for svc_name, keys in yaml_fields.items():
            tfields = (svc_trans.get(svc_name) or {}).get("fields") or {}
            tkeys = set(tfields.keys())
            missing = keys - tkeys
            label = f"[{lang}] 服务 {svc_name} 字段翻译齐全"
            if missing:
                label += f" (缺失: {sorted(missing)})"
            check(label, not missing)
