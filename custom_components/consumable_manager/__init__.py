"""耗材管理器 集成入口平台。"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, event, translation
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN, LOGGER, NOTIFY_DEFAULT_SCHEDULE_TIME, PLATFORMS,
    CONF_ENTRY_TYPE, CONF_NOTIFICATION, CONF_NOTIFY_MODE,
    CONF_NOTIFY_SCHEDULE_TIME, ENTRY_TYPE_NOTIFICATION,
    CONSUMABLE_UNITS, NOTIFY_MODE_SCHEDULED, NOTIFY_TEXTS, TODO_KINDS,
)
from . import bindings
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

async def _async_labels(hass: HomeAssistant) -> dict[str, str]:
    """运行文案标签（翻译缓存；todo_kind + notify_text + 计量单位共用一张表）。"""
    translations = translation.async_get_cached_translations(
        hass, hass.config.language, "selector", integration=DOMAIN
    )
    if not translations:
        # 冗余兜底：启动极早期翻译缓存可能尚未预热（返回空表），
        try:
            translations = await translation.async_get_translations(
                hass, hass.config.language, "selector", [DOMAIN]
            )
        except Exception:  # noqa: BLE001 —— 翻译加载失败不影响集成运行
            translations = {}
    labels: dict[str, str] = {}
    for kind in TODO_KINDS:
        labels[kind] = translations.get(
            f"component.{DOMAIN}.selector.todo_kind.options.{kind}", kind
        )
    for key in NOTIFY_TEXTS:
        labels[key] = translations.get(
            f"component.{DOMAIN}.selector.notify_text.options.{key}", key
        )
    for unit in CONSUMABLE_UNITS:
        labels[f"unit.{unit}"] = translations.get(
            f"component.{DOMAIN}.selector.units.options.{unit}", unit
        )
    return labels

def _async_register_notify_schedule(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> None:
    """注册每天定时推送（本地时刻，second=0 每天只触发一次）。"""
    def _parse(schedule: str) -> tuple[int, int] | None:
        try:
            hour, minute = (int(part) for part in schedule.split(":"))
            return hour, minute
        except ValueError:
            return None

    def _track(hour: int, minute: int, callback) -> None:
        try:
            entry.async_on_unload(
                # second=0 必传：缺省 second 会展开为 0-59 全部秒，
                # 回调在目标分钟内每秒触发一次。async_track_time_change
                # 按本地时间匹配，跨夏令时自动跟随（无需手工换 UTC）。
                event.async_track_time_change(
                    hass, callback, hour=hour, minute=minute, second=0
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
    """设置条目：预热绑定缓存 → 建协调器 → 首次刷新 → 记签名 → 注册监听 → 转发平台。"""
    # 绑定缓存必须在建协调器 / 实体属性计算之前异步预热：否则首次
    # get_binding 会落在事件循环内同步读盘，触发 HA 的 blocking call 告警。
    await bindings.async_prime(hass)
    labels = await _async_labels(hass)
    # 类型元数据（内置库+用户库合并；自定义类型不在库中时为 None）
    library = await async_load_library(hass)
    type_meta = library.type_meta(entry.data.get(CONF_ENTRY_TYPE, ""))
    coordinator = build_coordinator(hass, entry, labels, type_meta, library)
    # 首次刷新建立数据基线；实体订阅使「改实体值」即时刷新，定时轮询由
    # DataUpdateCoordinator 在实体加入监听时自动启动
    await coordinator.async_config_entry_first_refresh()
    unsub = coordinator.async_subscribe()
    if unsub is not None:
        entry.async_on_unload(unsub)
    entry.runtime_data = ConsumableManagerData(
        coordinator=coordinator,
        entity_signature=coordinator.entity_signature,
        notification_section=_notification_snapshot(entry),
    )

    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    _async_register_notify_schedule(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

def _notification_snapshot(
    entry: ConsumableManagerConfigEntry,
) -> dict[str, Any] | None:
    """条目通知段快照（浅拷贝；无段返回 None），供 update listener 变更比对。"""
    section = entry.options.get(CONF_NOTIFICATION)
    return dict(section) if isinstance(section, dict) else None

async def async_update_listener(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> None:
    """选项变更：通知段变化才重载（快照比对）；其余按实体签名重载或刷新。"""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    runtime = entry.runtime_data
    snapshot = _notification_snapshot(entry)
    if snapshot != getattr(runtime, "notification_section", None):
        runtime.notification_section = snapshot
        await hass.config_entries.async_reload(entry.entry_id)
        return
    coordinator = runtime.coordinator
    if coordinator.entity_signature != runtime.entity_signature:
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        await coordinator.async_request_refresh()

async def async_unload_entry(hass: HomeAssistant,
    entry: ConsumableManagerConfigEntry,
) -> bool:
    """卸载配置条目：卸载平台后停止协调器（实体订阅 + 定时轮询）。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = getattr(entry.runtime_data, "coordinator", None)
        if coordinator is not None:
            await coordinator.async_shutdown()
    return unload_ok