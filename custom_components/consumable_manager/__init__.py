"""耗材管理器 集成入口平台。"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, event, translation
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, LOGGER, NOTIFY_DEFAULT_SCHEDULE_TIME, PLATFORMS,
    CONF_ENTRY_TYPE, CONF_NOTIFICATION, CONF_NOTIFY_MODE,
    CONF_NOTIFY_SCHEDULE_TIME, ENTRY_TYPE_NOTIFICATION,
    NOTIFY_MODE_SCHEDULED, NOTIFY_TEXTS, TODO_KINDS,
)
from .coordinator import ConsumableManagerData, build_coordinator
from .notifications import (
    _scheduled_override,
    async_flush_entry,
    async_scheduled_flush,
)
from .services import async_setup_services
from .user_library import async_load_library

# 全集成统一的强类型条目别名（PEP 695），所有模块从本包导入
type ConsumableManagerConfigEntry = ConfigEntry[ConsumableManagerData]

# 仅支持界面配置（无 YAML 配置项），消除 hassfest CONFIG_SCHEMA 警告
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

def _async_labels(hass: HomeAssistant) -> dict[str, str]:
    """运行文案标签（翻译缓存；todo_kind + notify_text 共用一张表）。"""
    translations = translation.async_get_cached_translations(
        hass, hass.config.language, "selector"
    )
    labels: dict[str, str] = {}
    for kind in TODO_KINDS:
        labels[kind] = translations.get(
            f"component.{DOMAIN}.selector.todo_kind.options.{kind}", kind
        )
    for key in NOTIFY_TEXTS:
        labels[key] = translations.get(
            f"component.{DOMAIN}.selector.notify_text.options.{key}", key
        )
    return labels

def _async_register_notify_schedule(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> None:
    """注册每天定时推送（本地时刻 → UTC）。"""
    def _parse(schedule: str) -> tuple[int, int] | None:
        try:
            hour, minute = (int(part) for part in schedule.split(":"))
            return hour, minute
        except ValueError:
            return None

    def _track(hour: int, minute: int, callback) -> None:
        try:
            local_dt = dt_util.now().replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            utc_dt = dt_util.as_utc(local_dt)
            entry.async_on_unload(
                event.async_track_utc_time_change(
                    hass, callback, hour=utc_dt.hour, minute=utc_dt.minute
                )
            )
        except Exception:  # noqa: BLE001 —— 调度注册失败不影响集成运行
            LOGGER.warning("定时推送调度注册失败（%02d:%02d）", hour, minute)

    section = entry.options.get(CONF_NOTIFICATION) or {}
    mode = section.get(CONF_NOTIFY_MODE)
    schedule = str(section.get(CONF_NOTIFY_SCHEDULE_TIME, "") or "")

    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION:
        # 全局合并推送（全局模式为定时时）
        if mode == NOTIFY_MODE_SCHEDULED and (parsed := _parse(schedule)):
            async def _flush(_now) -> None:
                await async_scheduled_flush(hass)

            _track(*parsed, _flush)
        return

    # 业务条目：条目级覆盖定时 → 独立定时推送
    if mode != NOTIFY_MODE_SCHEDULED:
        return
    override = _scheduled_override(entry.options)
    entry_time = (
        (override[1] if override else schedule)
        or schedule
        or NOTIFY_DEFAULT_SCHEDULE_TIME
    )
    parsed = _parse(entry_time)
    if parsed is None:
        return

    async def _flush_entry(_now) -> None:
        await async_flush_entry(hass, entry)

    _track(*parsed, _flush_entry)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """集成整体初始化：注册服务（全局 + 实体）。"""
    await async_setup_services(hass)
    return True

async def async_setup_entry(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> bool:
    """设置条目：建协调器 → 首次刷新 → 记签名 → 注册监听 → 转发平台。"""
    labels = _async_labels(hass)
    # 类型元数据（内置库+用户库合并；自定义类型不在库中时为 None）
    library = await async_load_library(hass)
    type_meta = library.type_meta(entry.data.get(CONF_ENTRY_TYPE, ""))
    coordinator = build_coordinator(hass, entry, labels, type_meta, library)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = ConsumableManagerData(
        coordinator=coordinator,
        entity_signature=coordinator.entity_signature,
    )

    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    _async_register_notify_schedule(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_update_listener(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> None:
    """选项变更：通知配置一律重载；其余按实体签名重载或刷新。"""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    if CONF_NOTIFICATION in entry.options:
        # 条目级通知段存在（新增 / 修改覆盖）→ 重载刷新定时调度
        await hass.config_entries.async_reload(entry.entry_id)
        return
    coordinator = entry.runtime_data.coordinator
    if coordinator.entity_signature != entry.runtime_data.entity_signature:
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        await coordinator.async_request_refresh()

async def async_unload_entry(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> bool:
    """卸载配置条目（平台卸载即完成清理）。"""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
