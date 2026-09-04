<div align="right">
  <strong>简体中文</strong> | <a href="./README.md">English</a>
</div>

# <img src="custom_components/consumable_manager/brand/icon.png" width="64"> 📦 耗材管理器 (Consumable Manager)

[![Release](https://img.shields.io/github/v/release/PraxiGEN/ha_consumable_manager)](https://github.com/PraxiGEN/ha_consumable_manager/releases)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/PraxiGEN/ha_consumable_manager/blob/main/LICENSE)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

## 耗材管理器不只是库存计数器——它把「设备耗材状态监控、库存管理、更换待办、到期提醒通知、自动化服务」整合成一套完整的家庭耗材生命周期管理体系。所有数据本地离线运行，不依赖任何云服务。

## ✨ 核心特性

### 系统使用本地内置库作为数据源，所有逻辑均在本地离线计算完成。

### 🗂️ 耗材类型监控 (Monitoring)
- **内置类型**：开箱即用——电池、净化设备滤芯、打印机耗材、扫地机耗材、净水器耗材。🔋
- **自定义类型**：添加界面内置类型向导，键名、图标、默认阈值一站配齐，与内置类型平起平坐。🧩
- **阈值判定**：剩余寿命（%）、剩余时间、已使用时长三种阈值类型；大于 / 小于 / 等于三种触发方式；单位支持 % / 分钟 / 小时 / 天。
- **批量绑定**：正则表达式一次性批量匹配实体，或手动多选；**支持绑定群组（group.xxx），成员自动展开监控、动态跟随增减**。🔭
- **分组监控**：一个条目内可建多个分组——绑定实体分组（手动多选 + 正则动态入组，可为每组单独设阈值）与自定义耗材实体（自建倒计时数据，按已使用时长判定）。分组各自生成诊断与数据传感器。🧮
- **多语言**：内置库类型 / 耗材 / 设备名称按 HA 界面语言自动切换，内置中英双语。

### 📦 库存管理 (Stock)
- **库存项台账**：每个耗材一条库存项——名称、关联类型、型号、单位、数量、库存阈值。
- **常用耗材一键入库**：从内置库下拉选择，名称 / 单位 / 图标自动带出。
- **更换自动扣减**：勾选「更换」待办即记录更换时间并自动扣减关联库存项数量。➖
- **欠货负库存**：数量可为负（表示欠货），欠货时实体切换警示图标。

### ✅ 待办联动 (Todo)
- **更换待办**：耗材越过阈值自动生成「更换 XX」待办，勾选完成 = 已更换。🔄
- **购买待办**：库存低于阈值自动生成「购买」待办，清单化采购。🛒
- **原生体验**：基于 HA 原生待办平台，支持手机桌面小组件、日期设置、手动增删。

### 🔔 通知推送 (Notify)
- **双模式**：实时推送（触发即通知）/ 定时统一推送（每天固定时刻合并成一条）。⏰
- **双样式**：人性化文案（「书房温湿度传感器电量低，请更换。」）/ 状态值（「书房温湿度传感器 18%」）。💬
- **双渠道**：HA 系统通知 + notify 通知服务实体（App、短信网关等，可多选）。
- **条目级覆盖**：全局统一配置，单个条目可按需覆盖渠道 / 样式 / 模式，甚至独立定时。

### 📚 双库架构 (Library)
- **内置库**：随集成分发的类型 / 耗材数据（`library/` 目录，中英多语言）。
- **用户库**：本地 `config/.consumable_manager/user_library.json`，服务、配置界面均可写入，支持手工编辑。
- **贡献回流**：用户库即贡献草稿——把用户库 PR 到 main 分支的 `contributions/<你的GitHub用户名>/` 目录，GitHub Actions 自动验证并组装进内置库，人人可丰富公共数据。🤝

### 🔌 自动化服务 (Services)
- 7 个原生服务：绑定实体、解绑实体、查询绑定、添加耗材、添加类型、查询数据、调整库存——配置界面与自动化脚本双入口。⚙️

## 📦 安装

### 通过 HACS 安装（推荐）

1. 在 HACS 的"集成"部分，点击右上角的三点菜单
2. 选择"自定义存储库"
3. 在存储库字段输入：
```yaml
https://github.com/PraxiGEN/ha_consumable_manager
```
4. 类别选择"集成"
5. 点击"添加"保存
6. 在 HACS 中找到"Consumable Manager"集成并点击安装
7. 重启 Home Assistant

### 手动安装

1. 下载最新的：
```yaml
https://github.com/PraxiGEN/ha_consumable_manager
```
2. 解压并将 `custom_components/consumable_manager` 文件夹放入 Home Assistant 的 `custom_components` 目录
3. 重启 Home Assistant

## 📖 文档导航
- [🚀 详细配置与使用教程 (md/DOCS_CN.md)](md/DOCS_CN.md)
- [📜 版本更新历史 (md/CHANGELOG_CN.md)](md/CHANGELOG_CN.md)
- [📚 耗材库结构与贡献指南 (library/README.md)](custom_components/consumable_manager/library/README.md)

## 🤝 贡献

欢迎贡献代码、报告问题、提出功能建议或贡献耗材数据！

1. **提交 Issues**：报告问题或功能请求
2. **提交 Pull Requests**：贡献代码改进
3. **贡献耗材数据**：把你的用户库（`config/.consumable_manager/user_library.json`）PR 到 main 分支的 `contributions/<你的GitHub用户名>/` 目录，Actions 会自动验证并组装进内置库，详见 [贡献指南](custom_components/consumable_manager/library/README.md)

## 📄 许可证

本项目基于 MIT 许可证开源。详情请查看 LICENSE 文件。

## ❤️ 支持

如果这个项目对您有帮助，请给项目点个 Star ⭐！

---

## 兼容版本: 

- **Home Assistant 2026.1+**
  
  本集成最低兼容 HA 2026.1 及以上版本。

- **为确保集成品牌图片正确显示，请选择 Home Assistant 2026.3+**
  
  为确保品牌图标能够正确显示，建议使用 HA 2026.3 或更高版本。
  从 2026.3 起，Home Assistant 引入了 custom_integrations 目录与 Brands Proxy API，自定义集成可以在自身目录中直接包含品牌图片。