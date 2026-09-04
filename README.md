<div align="right">
  <strong>English</strong> | <a href="./README_CN.md">中文版</a>
</div>

# <img src="custom_components/consumable_manager/brand/icon.png" width="64"> 📦 Consumable Manager

[![Release](https://img.shields.io/github/v/release/PraxiGEN/ha_consumable_manager)](https://github.com/PraxiGEN/ha_consumable_manager/releases)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/PraxiGEN/ha_consumable_manager/blob/main/LICENSE)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

## Consumable Manager is more than an inventory counter — it combines "device consumable status monitoring, stock management, replacement to-dos, due-date reminder notifications, and automation services" into one complete household consumable lifecycle management system. All data is processed locally and offline, with no dependency on any cloud service.

## ✨ Core Features

### The system uses a local built-in library as its data source; all logic is computed locally and offline.

### 🗂️ Consumable Type Monitoring
- **Built-in types**: ready out of the box — batteries, purifier filters, printer consumables, robot vacuum consumables, water purifier consumables. 🔋
- **Custom types**: the add dialog ships with a built-in type wizard — key, icon, and default threshold configured in one go, on equal footing with built-in types. 🧩
- **Threshold rules**: three threshold types (remaining life %, remaining time, elapsed usage time); three trigger modes (greater than / less than / equal); units support % / minutes / hours / days.
- **Batch binding**: match entities in bulk with a regular expression, or pick them manually; **group entities (`group.xxx`) are supported — members are automatically expanded for monitoring and dynamically followed as they change**. 🔭
- **Group monitoring**: create multiple groups within one entry — binding-entity groups (manual multi-select + regex dynamic enrollment, with per-group thresholds) and custom consumable entities (self-built countdown data, evaluated by elapsed usage). Each group gets its own diagnostic and data sensors. 🧮
- **Multi-language**: built-in library type / consumable / device names switch automatically with the HA interface language; Chinese and English included.

### 📦 Stock Management
- **Stock ledger**: one stock item per consumable — name, linked type, model, unit, quantity, and stock threshold.
- **One-click common consumables**: pick from the built-in library dropdown; name / unit / icon are filled in automatically.
- **Automatic deduction on replacement**: checking the "Replace" to-do records the replacement time and automatically deducts the linked stock item. ➖
- **Negative stock (backorder)**: quantity may go negative (indicating a backorder); the entity switches to a warning icon when in backorder.

### ✅ To-do Integration
- **Replacement to-dos**: crossing a threshold automatically creates a "Replace XX" to-do; checking it off means replaced. 🔄
- **Purchase to-dos**: stock below threshold automatically creates a "Purchase" to-do for checklist-style procurement. 🛒
- **Native experience**: built on the HA native to-do platform, with home-screen widgets, due dates, and manual add/remove support.

### 🔔 Notifications
- **Dual modes**: real-time push (notify on trigger) / scheduled digest (merge all alerts into one message at a fixed time each day). ⏰
- **Dual styles**: human-friendly text ("Study temp-humidity sensor battery is low, please replace.") / state values ("Study temp-humidity sensor 18%"). 💬
- **Dual channels**: HA system notification + notify service entities (mobile app, SMS gateway, etc., multi-select).
- **Per-entry overrides**: global defaults with per-entry overrides of channel / style / mode — even an independent schedule.

### 📚 Dual-Library Architecture
- **Built-in library**: type / consumable data shipped with the integration (`library/` directory, Chinese & English). 
- **User library**: local `config/.consumable_manager/user_library.json`, writable from services and the config UI, and editable by hand.
- **Contribution loop**: your user library doubles as a contribution draft — PR it to `contributions/<your-github-username>/` on the main branch, and GitHub Actions assembles it into the built-in library automatically. 🤝

### 🔌 Automation Services
- 7 native services: Bind Entity, Unbind Entity, Query Bindings, Add Consumable, Add Type, Query Data, Adjust Stock — accessible from both the config UI and automations/scripts. ⚙️

## 📦 Installation

### Via HACS (recommended)

1. In HACS, under "Integrations", click the three-dot menu at the top right
2. Select "Custom repositories"
3. Enter this URL in the repository field:
```yaml
https://github.com/PraxiGEN/ha_consumable_manager
```
4. Choose "Integration" as the category
5. Click "Add" to save
6. Find "Consumable Manager" in HACS and click install
7. Restart Home Assistant

### Manual installation

1. Download the latest release:
```yaml
https://github.com/PraxiGEN/ha_consumable_manager
```
2. Extract and place the `custom_components/consumable_manager` folder into Home Assistant's `custom_components` directory
3. Restart Home Assistant

## 📖 Documentation
- [🚀 Detailed Configuration & Usage Guide (md/DOCS.md)](md/DOCS.md)
- [📜 Changelog (md/CHANGELOG.md)](md/CHANGELOG.md)
- [📚 Library Structure & Contribution Guide (library/README.md)](custom_components/consumable_manager/library/README.md)

## 🤝 Contributing

Contributions of code, bug reports, feature suggestions, and consumable data are all welcome!

1. **Open Issues**: report problems or request features
2. **Open Pull Requests**: contribute code improvements
3. **Contribute consumable data**: PR your user library (`config/.consumable_manager/user_library.json`) to `contributions/<your-github-username>/` on the main branch — Actions will validate and assemble it into the built-in library automatically. See the [contribution guide](custom_components/consumable_manager/library/README.md)

## 📄 License

This project is open-sourced under the MIT License. See the LICENSE file for details.

## ❤️ Support

If this project helps you, please give it a Star ⭐!

---

## Compatibility:

- **Home Assistant 2026.1+**

  This integration requires HA 2026.1 or later.

- **For the integration brand image to display correctly, use Home Assistant 2026.3+**

  To ensure the brand icon displays correctly, HA 2026.3 or later is recommended.
  Starting from 2026.3, Home Assistant introduced the custom_integrations directory and Brands Proxy API, allowing custom integrations to include brand images in their own directory.
