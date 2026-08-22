# 内置耗材库 / Built-in Consumable Library

---

## 中文

本目录是 `consumable_manager` 集成的内置耗材库。内置库是**数据**，不是代码——
社区通过 PR 贡献耗材 / 类型 / 设备映射，由 CI 自动组装进这里的 JSON 文件。

### 仓库结构

```
<repo>/
  custom_components/consumable_manager/    # 集成包（含本库目录），HA 安装只复制这里
    library/                               # 内置库（本目录，CI 的写入目标）
  tools/ingest.py                          # 摄入 / 检查工具（仅 CI 使用）
  contributions/<GitHub用户名>/user_library.json   # 贡献草稿（分支中的文件夹）
  .github/workflows/ingest-contributions.yml
```

### 贡献方式：只读分支中的文件夹

**摄入工具只读取 git 分支中的 `contributions/` 文件夹，不在本地使用。**
贡献流程中没有任何本地运行工具的环节：

1. Fork 并克隆本仓库。
2. 把你在 Home Assistant 界面 / 服务中添加的内容（本地
   `config/.consumable_manager/user_library.json`）复制为
   `contributions/<你的GitHub用户名>/user_library.json`。
   每个贡献者使用自己的目录，**并行 PR 不会冲突**。
   `name` 可填多语言 dict，如 `{"zh-Hans": "…", "en": "…"}`。
3. PR 到 `contributions` 分支，只提交你自己目录下的 `user_library.json`。
4. **之后全自动**：GitHub Actions 在分支上运行摄入工具——只读取
   `contributions/` 文件夹下的全部草稿（`*/user_library.json`），
   新增条目合并进 `library/`（多语言拆解进 `names.json`）、排序归位、
   同锚点冲突项标记人工裁决（不覆盖），组装后移除整个 `contributions/`
   目录并提交回 `contributions` 分支。
5. 维护者审查组装结果后，将 `contributions` 分支合入主分支。
   主分支的净变化仅为 `library/`——内置库始终保持
   「已检查、已排序、无冲突、无草稿」状态。

注意：`user_library.json` 是公开仓库中的文件，提交前请自行检查 `meta`
等字段，移除任何不想公开的个人信息。

### 目录结构

```
library/
  index.json            类型元数据表：schema_version + types（类型键 → 图标 / 阈值默认值）
  consumables.json      耗材扁平数组：定义「耗材是什么」（显式 type 字段）
  devices.json          设备映射扁平数组：定义「设备用了啥」（models[] 数组合并同系列变体）
  names.json            多语言映射表（可选附属文件）：types / consumables / devices 三段
```

### 铁律

- **顺序无语义**：加载器对条目顺序零敏感，合并与查找只靠锚点
  （类型 key / 耗材 id / manufacturer+models）。文件内排序只为便于阅读与
  git diff，由 CI 工具维持。
- **全字段必填**：`meta` 可为空对象 `{}`；校验只查「有且合法」。
- **单向回流**：`contributions/` 草稿 → 内置库，永不反向同步。

### 字段规格（schema v1）

consumables.json 每条（六字段全必填）：

| 字段 | 说明 |
|------|------|
| `id` | 全局唯一，`^[a-z0-9_]+$`，约定 `<type>_<slug>`，如 `filter_hepa13` |
| `type` | 耗材类型，必须在 index.json 的 types 中定义 |
| `model` | 型号（国际通用编号，永不翻译） |
| `name` | 显示名，**英文 plain 兜底**（多语言见 names.json） |
| `unit` | 计量单位（个 / 节 / 粒 …） |
| `meta` | 规格对象，可为 `{}` |

devices.json 每条（四字段全必填）：`manufacturer` / `models[]`（非空字符串数组，
同系列变体合并）/ `name`（英文 plain 兜底）/ `consumables[]`（引用的耗材 id，须已定义）。

index.json：`schema_version`（=1）+ `types`（类型键 → `name` / `icon` /
`default_threshold_type` / `default_threshold` / `default_threshold_unit`，全必填）。

### 多语言

- 数据文件 `name` 一律**英文 plain**（可读兜底）；具体语言的显示名放 `names.json`。
- `names.json` 三段，key 规则：`types` 用类型键；`consumables` 用耗材 id；
  `devices` 用 `manufacturer_model`（规范化小写拼接，如 `xiaomi_zhimi_airpurifier_m3`）。
- 解析回退链：names 映射 → 数据内 name → model / 类型键，任何语言下都有显示。
- 新增语言只需在 `names.json` 对应 key 上补一个语言键，无需改数据文件。

### 排序规范（CI 维持，无需人工）

- `index.json` types：按 key 字母序。
- `consumables.json`：按 type 分组 + 组内 id 字母序。
- `devices.json`：按 manufacturer 再 model 字母序（忽略大小写）。
- `names.json`：各段按 key 字母序。
- 全部由 CI 中的 `tools/ingest.py --ingest` 在摄入时自动归位；
  `--check` 负责校验（字段 / 引用 / 排序 / names 覆盖）。

---

## English

This directory holds the built-in consumable library of the `consumable_manager`
integration. The library is **data, not code** — the community contributes
consumables / types / device mappings via PR, and CI assembles them into these
JSON files automatically.

### Repository layout

```
<repo>/
  custom_components/consumable_manager/    # integration package (contains this library)
    library/                               # built-in library (this dir, CI write target)
  tools/ingest.py                          # ingest / check tool (CI only)
  contributions/<GitHub username>/user_library.json   # contribution drafts (folder in the branch)
  .github/workflows/ingest-contributions.yml
```

### How to contribute: the branch folder only

**The ingest tool reads only the `contributions/` folder in the git branch —
it is never used locally.** No step of the contribution flow runs anything on
your machine:

1. Fork and clone this repo.
2. Copy what you added via the Home Assistant UI / services (your local
   `config/.consumable_manager/user_library.json`) to
   `contributions/<your GitHub username>/user_library.json`.
   Each contributor has their own directory, so **parallel PRs never conflict**.
   `name` may be a locale dict, e.g. `{"zh-Hans": "…", "en": "…"}`.
3. Open a PR to the `contributions` branch, touching only your own
   `user_library.json`.
4. **Everything after that is automatic**: GitHub Actions runs the ingest tool
   on the branch — it reads only the drafts under the `contributions/` folder
   (`*/user_library.json`), merges new entries into `library/`
   (localization split into `names.json`), re-sorts everything, flags
   same-anchor conflicts for human decision (never overwrites), then removes
   the whole `contributions/` directory and commits the assembled result back
   to the `contributions` branch.
5. Maintainers review the assembled result and merge the `contributions`
   branch into main. The net change on main is purely `library/` — the
   built-in library always stays checked / sorted / conflict-free / draft-free.

Note: `user_library.json` lives in a public repo — review `meta` and other
fields before submitting, and remove anything you don't want public.

### Structure

```
library/
  index.json            type metadata table: schema_version + types (type key → icon / default thresholds)
  consumables.json      flat array of consumables (what a consumable is, explicit `type`)
  devices.json          flat array of device mappings (what a device uses, `models[]` merges variants)
  names.json            multilingual mapping table (optional): sections `types` / `consumables` / `devices`
```

### Iron rules

- **Order carries no meaning**: the loader is insensitive to entry order;
  merging and lookup rely only on anchors (type key / consumable id /
  manufacturer+models). In-file ordering exists for readability and git diffs,
  and is maintained by CI tooling.
- **All fields required**: `meta` may be an empty object `{}`; validation only
  checks "present and valid".
- **One-way ingestion**: `contributions/` drafts → built-in library, never
  the other way around.

### Field spec (schema v1)

Each consumables.json entry (six required fields):

| Field | Description |
|-------|-------------|
| `id` | globally unique, `^[a-z0-9_]+$`, convention `<type>_<slug>`, e.g. `filter_hepa13` |
| `type` | consumable type, must be defined in index.json types |
| `model` | model number (internationally used code, never translated) |
| `name` | display name, **English plain fallback** (see names.json for localization) |
| `unit` | unit of measure (pcs / cells / tablets …) |
| `meta` | spec object, may be `{}` |

Each devices.json entry (four required fields): `manufacturer` / `models[]`
(non-empty string array, variants of one series merged) / `name` (English plain
fallback) / `consumables[]` (referenced consumable ids, must be defined).

index.json: `schema_version` (=1) + `types` (type key → `name` / `icon` /
`default_threshold_type` / `default_threshold` / `default_threshold_unit`,
all required).

### Localization

- Data-file `name` is always **English plain text** (readable fallback);
  per-locale names live in `names.json`.
- `names.json` has three sections; key rules: `types` → type key;
  `consumables` → consumable id; `devices` → `manufacturer_model`
  (lowercased slug join, e.g. `xiaomi_zhimi_airpurifier_m3`).
- Resolution fallback: names map → data-file name → model / type key
  (never empty).
- Adding a language = adding one locale key in `names.json`; no data-file
  changes needed.

### Ordering convention (maintained by CI, not by hand)

- `index.json` types: alphabetical by key.
- `consumables.json`: grouped by type, then alphabetical by id within a group.
- `devices.json`: alphabetical by manufacturer, then model (case-insensitive).
- `names.json`: alphabetical by key within each section.
- All of this is handled automatically by `tools/ingest.py --ingest` in CI;
  `--check` validates it (fields / references / ordering / names coverage).
