# 📖 Documentation

## ⚙️ Configuration

### Step 1: Add the integration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for and select **Consumable Manager**.
3. In the "Choose entry type" dialog, pick the entry type to add:

| Entry type | Description |
| :--- | :--- |
| ⏰ **Notification settings** | Global notification config (channels / mode / style); generates no entities. |
| 📦 **Stock management** | Stock ledger: add / edit / remove stock items, quantities, and thresholds. |
| 🗂️ **Consumable type** (battery / purifier filter / printer consumable / robot vacuum consumable / water purifier consumable…) | Bind device entities, monitor consumable status, and generate replacement to-dos. The list comes from the built-in + user libraries. |
| 🧩 **Custom type** | Type wizard: enter the type key, name, icon, and default threshold; written to the user library before the entry is created. |

> **Each type allows only one entry** (adding a duplicate shows an "already configured" message). It is recommended to add "Notification settings" first, then business entries.
> Entries are ordered on the integration page as: ⏰ Notification settings → 📦 Stock management → each consumable type entry.

### Step 2: Configure a consumable type entry

After adding a "Consumable type" entry, click it to open the config menu:

**Create group → Edit group → Remove groups → Set threshold → Notification settings**. Monitoring is **group-based** — every group independently generates a diagnostic entity and a data sensor, and each group can override the entry-level threshold.

#### Create group (two kinds)

**① Binding-entity group** (linked sensors / groups, evaluated by entity values):
- **Entity multi-select**: tick the entities to monitor (e.g. `sensor.air_purifier_filter_life`).
- **Regex batch matching**: enter a regular expression that is dynamically matched against current entity IDs at runtime (disabled entities are excluded automatically), e.g. `sensor\..*filter.*`; **newly matching entities join the group automatically with no reconfiguration**.
- **Group binding**: you can pick a **group** helper (`group.xxx`); on every refresh the group's members are expanded into actual entities and evaluated one by one, and membership changes are followed automatically.
- Manual multi-select and regex matches are merged as a **union** on submit.
- Optionally tick **override entry-level threshold** to set a separate threshold type / value / unit / operator for this group.

**② Custom consumable entity** (self-built countdown data, no entity binding required):
- Enter a name, an optional bound consumable, the **added/replaced date**, and the **expected lifespan** (days / hours / minutes); a countdown data entity is generated automatically.
- It can likewise override the entry-level threshold and is evaluated by elapsed usage time.

#### Threshold settings (entry level)
| Field | Description |
| :--- | :--- |
| **Threshold type** | Remaining life (%) / remaining time / elapsed usage — determined by the bound entity's semantics. |
| **Threshold** | The value that triggers an alert. |
| **Unit** | % / minutes / hours / days (for time-based types). |
| **Operator** | Greater than / less than / equal — crossing the condition marks it as "needs replacement". |

> Threshold defaults fall back in three levels: stored entry config → library type metadata (default threshold from the built-in library or the custom type wizard) → generic fallback (remaining life 20%). When a group overrides the threshold, the group value wins.

### Step 3: Configure the stock entry

The "Stock management" entry config menu: **Add item → Edit item → Remove items → Notification settings**.

#### Adding a stock item (two ways)
- **Common consumable**: pick the linked consumable type → choose a consumable from the built-in / user library dropdown → enter quantity and stock threshold. Name and unit are filled in automatically.
- **Custom**: manually enter name, linked consumable type (required), model, unit, quantity, and stock threshold. On submit it is written to the local user library (available in the dropdown next time).

| Field | Description |
| :--- | :--- |
| **Name** | The stock item's display name (also the entity name). |
| **Linked consumable type** | Required — decides the icon and the deduction link on replacement. |
| **Model** | Only in the custom flow (letters / digits); used for consumable ID generation and user-library deduplication. |
| **Unit** | Selected from a dropdown of standard units (piece / sheet / bottle…), translated automatically. |
| **Quantity** | May be negative (negative means backorder; the entity switches to a warning icon). |
| **Stock threshold** | Below this quantity a "low stock" alert and a purchase to-do are generated. |

#### Editing a stock item
- Only **quantity** and **stock threshold** may be changed; to change the name / model / unit, delete the item and add it again.

#### Removing stock items
- Multi-select batch removal.

### Step 4: Configure notifications

#### Global notifications (⏰ Notification settings entry)
Click the "Notification settings" entry to open the form directly:

| Field | Description |
| :--- | :--- |
| **HA system notification** | Pushed as persistent notifications (HA notification center). |
| **Notify entities** | Pick notify-domain entities (mobile app, SMS gateway, etc.), multi-select. |
| **Message style** | `Human-friendly text`: "Study temp-humidity sensor battery is low, please replace." / `State value`: "Study temp-humidity sensor 18%". |
| **Push mode** | `Real-time`: pushed the moment the state flips / `Scheduled`: all alerts merged into one digest at a fixed time each day. |
| **Schedule time** | Only effective in scheduled mode; defaults to 20:00. |

> At least one notification channel must be selected, otherwise the form cannot be saved.

#### Per-entry overrides ("Notification settings" menu item on every business entry)
- Enable "Customize notifications for this entry" to configure channel / style / mode per entry, overriding the global settings; turning it off deletes the override section and falls back to global.
- Per-entry **independent scheduling** is also available (pick any time, leave empty to follow the global time) — pushed independently, not merged into the global digest.
- Notifications are **edge-triggered**: sent only once at the "normal → abnormal" transition; sustained abnormality does not re-notify, and entries already abnormal at startup do not backfill (preventing restart floods).

---

## 🛠️ Entities

### Stock management entry
| Entity | Description |
| :--- | :--- |
| `sensor.{item name}` | **State**: stock quantity. **Attributes**: linked consumable type, unit, stock threshold, shortage flag, etc. **Icon**: from the linked type's library metadata; switches to a warning icon in backorder. |
| `sensor.{name} stock status` | **State**: `ok` / `low stock` (enum, diagnostic). **Attributes**: item count, list of short items. |
| `todo.{name} to-do` | Purchase to-do: below the stock threshold a "Purchase XX" to-do is created automatically; check it off when procured. |

### Consumable type entry
Each **group** generates a pair of entities (diagnostic + data); custom consumable entity groups additionally get a countdown entity:
| Entity | Description |
| :--- | :--- |
| `sensor.{group name} replace status` | **State**: `ok` / `needs replacement` (enum, diagnostic; one per group). **Attributes**: consumable type, group, triggering entities, threshold (type/value/unit/operator), last replaced time, bound consumable info. |
| `sensor.{group name} data` | **State**: the minimum monitored value within the group (a plain number without a unit, easy to compare in automations). **Attributes**: group, consumable type, each bound entity's live value (including bound consumables). |
| `sensor.{custom name} (countdown)` | Custom consumable entity groups only — **State**: remaining lifespan (in the lifespan unit d / h / min, `device_class: duration`). **Attributes**: expected lifespan, added/replaced dates. |
| `todo.{name} to-do` | Replacement to-do: any group crossing its threshold creates a "Replace XX" to-do; **checking it off = replaced** (records the replacement time + automatically deducts the linked stock). |

> The notification settings entry generates no entities; entries with no bound entities / no stock items likewise generate no entities.

---

## 🧰 Services

All services are available in **Developer Tools → Actions** (forms driven by services.yaml) and in automations / scripts.

### consumable_manager.adjust_stock — Adjust stock
Increase or decrease a stock item's quantity (the usual entry point for automatic stock deduction after replacing a consumable in an automation).
```yaml
service: consumable_manager.adjust_stock
data:
  action: consume   # add / consume
  item: sensor.study_filter   # stock item entity or item_id
  quantity: 1
```

### consumable_manager.query_data — Query data
Extract structured data from this integration (JSON, **response-only service**, used with `response_variable`), suitable for dashboards or external systems. `data_type` is required:
| data_type | Returns |
| :--- | :--- |
| `stock` | Stock items (filterable by item) |
| `type_entry` | Consumable type entry states |
| `group_data` | Group entity data (members include bound consumable fields) |
| `types` | Consumable type metadata |
| `consumables` | All consumables (filterable by type) |

```yaml
service: consumable_manager.query_data
data:
  data_type: stock
response_variable: result
```

### consumable_manager.bind_entity — Bind an entity to a consumable
Bind a device consumable entity to a consumable in the library (**pure metadata mapping**, written to an independent binding layer, used only for to-do / notification display; does not trigger monitoring). Specify `consumable_id` manually, or pass only the linked stock item `item` (inherits the consumable linked to that stock item).
```yaml
service: consumable_manager.bind_entity
data:
  entity_id: sensor.air_purifier_filter_life
  consumable_id: filter_hepa13   # consumable ID entered manually (text)
  # item: sensor.study_filter    # or pass a stock item entity to inherit its consumable
```

### consumable_manager.unbind_entity — Unbind an entity
Remove an entity's consumable binding mapping (only removes it from the independent binding layer; does not touch any entry's monitoring config).
```yaml
service: consumable_manager.unbind_entity
data:
  entity_id: sensor.air_purifier_filter_life
```

### consumable_manager.query_binding — Query bindings
Query entity ↔ consumable bindings (filter by entity, consumable, or stock item); the response includes each entity's threshold status (`triggered`).

### consumable_manager.add_consumable — Add a consumable to the user library
Add a consumable from template fields, written to the local user library. The consumable ID is generated automatically (type + model), e.g. `filter_hepa13`. `model` and `unit` must be English (ASCII; the unit must be one of the 18 standard keys such as `piece` / `filter`); the same fields in the config UI are dropdowns.
```yaml
service: consumable_manager.add_consumable
data:
  cons_type: filter
  model: HEPA-13
  name: HEPA 13 filter
  unit: piece
  meta: {"grade": "H13"}   # optional specs (any language)
```

### consumable_manager.add_type — Add a custom type
Add a custom consumable type to the local user library (equivalent to the type wizard in the config UI; handy for creating types in bulk via automations). The type key only allows lowercase letters, digits, and underscores, and must not duplicate an existing type.

---

## 📚 Library & Data Contributions

### Dual-library architecture and the binding layer
- **Built-in library** (`custom_components/consumable_manager/library/`): shipped with the integration — `index.json` (type metadata), `consumables.json` (consumables), `names.json` (multi-language mappings).
- **User library** (`config/.consumable_manager/user_library.json`): local data written by the config UI and services, and editable by hand (takes effect after restart). Entries with the same anchor: the user library wins and replaces the whole entry; a corrupt file is degraded and ignored with a warning.
- **Binding layer** (HA storage `.storage/consumable_manager.bindings`): entity ↔ consumable binding mappings, persisted independently of entry config, used only for displaying specs in to-dos / notifications; maintained by the "Bind Entity / Unbind Entity" services.

### Contribute your consumable data
Your user library is the contribution draft:

1. Copy `config/.consumable_manager/user_library.json` to `contributions/<your-github-username>/user_library.json` in the repo (check meta for sensitive data before submitting).
2. PR to the **main** branch.
3. GitHub Actions automatically runs the ingestion tool: field validation (code / identifier fields must be ASCII), anchor deduplication, multi-language splitting, sorting, and assembly into the built-in library, then pushes back to the PR (the draft is removed automatically).
4. Maintainers review and merge into the main branch.

See [library/README.md](custom_components/consumable_manager/library/README.md) (bilingual, shipped with the library).

---

## 💡 FAQ

#### Q: After binding a group, why do I see the member entities instead of the group itself?
#### A: A group entity's state is an aggregate value and cannot be evaluated against a threshold directly. On every refresh the integration expands group members into actual entities and evaluates them one by one; membership changes are followed automatically with no reconfiguration.

#### Q: Why didn't I receive an alert notification after a restart?
#### A: Notifications are edge-triggered — sent only at the "normal → abnormal" transition. Entries already abnormal at restart count as sustained abnormality and are not re-sent, preventing restart floods.

#### Q: What happens when I check off a "Replace" to-do?
#### A: It is equivalent to marking as replaced: the last-replaced time is recorded (persisted, survives restarts) and the linked stock item's quantity is deducted automatically; if it is linked to a "purchase" to-do instead, nothing is deducted.

#### Q: What if the user library file is corrupt?
#### A: The integration degrades gracefully, ignores the file, and logs a warning; the built-in library is unaffected. Repair or delete the file and restart to recover.

#### Q: Why doesn't the service dropdown show my custom type?
#### A: services.yaml is a static file; the dropdown only lists built-in types. Type the type key directly for custom types (custom value is enabled); the backend validates that it exists in the merged library.

#### Q: Where does the data go when I pick "Custom" when adding a stock item?
#### A: It is written to the local user library `config/.consumable_manager/user_library.json` (atomic writes) and is available in the dropdown next time; that file doubles as the contribution draft.
