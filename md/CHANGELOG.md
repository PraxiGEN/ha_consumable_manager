# 📜 Changelog

All notable changes to this project will be documented in this file.

## 0.1.16 — 2026-09-04

First public release: a complete household consumable lifecycle management system — device consumable monitoring, stock management, replacement to-dos, due-date notifications, and automation services. All data is processed locally and offline.

### Added

**Consumable type monitoring**
- 5 built-in types out of the box: battery, purifier filter, printer consumable, robot vacuum consumable, water purifier consumable.
- Custom type wizard: type key, name, icon, and default threshold in one flow, written to the user library and on equal footing with built-in types.
- Three threshold types (remaining life %, remaining time, elapsed usage) × three operators (greater / less / equal) with % / min / h / d units; three-level default fallback (entry → type metadata → generic 20%).
- Batch binding: manual multi-select, regex dynamic enrollment (newly matching entities join automatically), and `group.xxx` expansion with automatic membership following.

**Group monitoring**
- Multiple groups per type entry: binding-entity groups (multi-select + regex + group helpers, union on submit) and custom consumable entities (self-built countdown data, evaluated by elapsed usage).
- Per-group threshold override; each group generates its own diagnostic entity (replace status) and data sensor (group minimum, unitless); custom groups additionally generate a countdown entity (`device_class: duration`).

**Stock management**
- Stock ledger with name / linked type / model / unit / quantity / threshold; common consumables filled in from the library dropdown.
- Automatic stock deduction when a replacement to-do is checked off; negative quantity = backorder with a warning icon.
- Low-stock alerts with purchase to-dos; stock status diagnostic entity.

**To-do integration**
- Native HA to-do platform: "Replace XX" on threshold crossing, "Purchase XX" on low stock; supports home-screen widgets, due dates, and manual edits.

**Notifications**
- Dual channels (HA persistent notification + notify entities), dual styles (human-friendly text / state value), dual modes (real-time / daily digest at a fixed time, default 20:00).
- Global settings with per-entry overrides, including independent schedules; edge-triggered alerts with restart flood protection.

**Dual-library architecture**
- Built-in library shipped with the integration (`index.json` / `consumables.json` / `names.json`, Chinese & English); user library at `config/.consumable_manager/user_library.json` (atomic writes, hand-editable, user entries win on anchor conflicts).
- Independent binding layer (HA Store): entity ↔ consumable pure metadata mappings for to-do / notification display.

**Automation services (7)**
- `bind_entity`, `unbind_entity`, `query_binding`, `add_consumable`, `add_type`, `query_data`, `adjust_stock` — forms driven by services.yaml, usable in the UI and in automations/scripts; `query_data` is a response-only service with 5 data types.

**Contribution pipeline**
- PR your user library to `contributions/<username>/` on main; GitHub Actions validates (code / identifier fields must be ASCII), deduplicates by anchor, splits multi-language names, and assembles into the built-in library automatically.

**Compatibility**
- Home Assistant 2026.1+; brand icon display recommended on 2026.3+.
