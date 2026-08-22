"""耗材管理器 通知平台。"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import translation

from .const import (
    DOMAIN, CONF_ENTRY_TYPE, CONF_NOTIFICATION, CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_MODE, CONF_NOTIFY_SCHEDULE_TIME, CONF_NOTIFY_STYLE, CONF_NOTIFY_SYSTEM, 
    ENTRY_TYPE_NOTIFICATION, NOTIFY_MODE_REALTIME, NOTIFY_MODE_SCHEDULED, NOTIFY_STYLE_HUMAN,
)

# 通知服务（实体渠道走 notify.send_message + target）
_PERSISTENT_DOMAIN = "persistent_notification"
_NOTIFY_DOMAIN = "notify"
_NOTIFY_SERVICE = "send_message"
# 通知实体域（配置界面选择器过滤 + 运行时兜底校验）
NOTIFY_ENTITY_DOMAIN = "notify"
# 统一推送标题用集成名称（翻译键）
_INTEGRATION_TITLE_KEY = f"component.{DOMAIN}.title"

def normalize_notification_section(
    section: Any,
) -> dict[str, Any] | None:
    """把通知配置段规整为标准结构；无有效渠道时返回 None。"""
    if not isinstance(section, dict):
        return None
    entities = [
        str(entity_id)
        for entity_id in section.get(CONF_NOTIFY_ENTITIES, []) or []
        if entity_id
    ]
    system = bool(section.get(CONF_NOTIFY_SYSTEM, False))
    if not system and not entities:
        return None
    return {
        CONF_NOTIFY_SYSTEM: system,
        CONF_NOTIFY_ENTITIES: entities,
        CONF_NOTIFY_MODE: str(
            section.get(CONF_NOTIFY_MODE, NOTIFY_MODE_REALTIME)
        ),
        CONF_NOTIFY_SCHEDULE_TIME: str(
            section.get(CONF_NOTIFY_SCHEDULE_TIME, "") or ""
        ),
        CONF_NOTIFY_STYLE: str(
            section.get(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN)
        ),
    }

def _global_section(hass: HomeAssistant) -> dict[str, Any] | None:
    """全局通知条目的通知段（归一化后）。"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_NOTIFICATION:
            continue
        section = normalize_notification_section(
            entry.options.get(CONF_NOTIFICATION)
        )
        if section is not None:
            return section
    return None

def find_notification_config(hass: HomeAssistant,
    entry_options: dict[str, Any],
) -> dict[str, Any] | None:
    """查找生效的通知配置。"""
    global_section = _global_section(hass)
    override = normalize_notification_section(
        entry_options.get(CONF_NOTIFICATION)
    )
    if override is None:
        return global_section
    if global_section is None:
        return override
    merged = dict(global_section)
    merged[CONF_NOTIFY_SYSTEM] = override[CONF_NOTIFY_SYSTEM]
    merged[CONF_NOTIFY_ENTITIES] = override[CONF_NOTIFY_ENTITIES]
    merged[CONF_NOTIFY_STYLE] = override[CONF_NOTIFY_STYLE]
    return merged

async def _integration_title(hass: HomeAssistant, fallback: str) -> str:
    """集成名称（统一推送标题）；翻译获取失败回退。"""
    try:
        translations = await translation.async_get_translations(
            hass,
            hass.config.language,
            "component",
            integrations=[DOMAIN],
        )
        return translations.get(_INTEGRATION_TITLE_KEY, fallback)
    except Exception:  # noqa: BLE001 —— 翻译失败不应打断发送
        return fallback

def _scheduled_override( entry_options: dict[str, Any], ) -> tuple[str, str] | None:
    """条目级覆盖的定时设置：(mode, schedule_time)；未覆盖返回 None。"""
    section = entry_options.get(CONF_NOTIFICATION)
    if not isinstance(section, dict):
        return None
    if section.get(CONF_NOTIFY_MODE) != NOTIFY_MODE_SCHEDULED:
        return None
    return NOTIFY_MODE_SCHEDULED, str(section.get(CONF_NOTIFY_SCHEDULE_TIME) or "")

async def async_send_notification(
    hass: HomeAssistant,
    config: dict[str, Any],
    title: str,
    message: str,
    notification_id: str,
) -> None:
    """按配置双渠道发送通知（标题 / 消息已渲染完成）。"""
    if config[CONF_NOTIFY_SYSTEM]:
        await hass.services.async_call(
            _PERSISTENT_DOMAIN,
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": notification_id,
            },
            blocking=False,
        )

    entities = [e for e in config[CONF_NOTIFY_ENTITIES] if e]
    if entities:
        await hass.services.async_call(
            _NOTIFY_DOMAIN,
            _NOTIFY_SERVICE,
            {"title": title, "message": message},
            target={"entity_id": entities},
            blocking=False,
        )

def sanitize_notification_id(entry_id: str) -> str:
    """把条目 id 规整为合法 notification_id（小写字母数字下划线）。"""
    return re.sub(r"[^a-z0-9_]", "_", entry_id.lower()) or "consumable"

def _business_coordinators(hass: HomeAssistant) -> list[Any]:
    """所有业务条目（库存 / 耗材类型）的协调器（跳过通知条目）。"""
    coordinators: list[Any] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_NOTIFICATION:
            continue
        data = getattr(entry, "runtime_data", None)
        coordinator = getattr(data, "coordinator", None)
        if coordinator is not None:
            coordinators.append(coordinator)
    return coordinators

async def _send_pending(
    hass: HomeAssistant,
    pending: list[tuple[Any, dict[str, Any]]],
    title: str,
    notification_id: str,
) -> None:
    """把待推送列表合并为一条发送（渠道取并集），并清除待推送标记。"""
    lines: list[str] = []
    for coordinator, _config in pending:
        lines.append(f"{coordinator.title}：{coordinator._alert_text(_style_of(_config))}")
    message = "\n".join(lines)

    if any(config[CONF_NOTIFY_SYSTEM] for _c, config in pending):
        await hass.services.async_call(
            _PERSISTENT_DOMAIN,
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": notification_id,
            },
            blocking=False,
        )
    entities = sorted(
        {
            entity
            for _c, config in pending
            for entity in config[CONF_NOTIFY_ENTITIES]
            if entity
        }
    )
    if entities:
        await hass.services.async_call(
            _NOTIFY_DOMAIN,
            _NOTIFY_SERVICE,
            {"title": title, "message": message},
            target={"entity_id": entities},
            blocking=False,
        )

    for coordinator, _config in pending:
        coordinator.alert_pending = False

def _style_of(config: dict[str, Any]) -> str:
    return str(config.get(CONF_NOTIFY_STYLE, NOTIFY_STYLE_HUMAN))

async def async_scheduled_flush(hass: HomeAssistant) -> None:
    """全局定时统一推送：合并「跟随全局定时」且有待推送的条目（标题 = 集成名称）。"""
    global_section = _global_section(hass)
    if global_section is None:
        return

    pending: list[tuple[Any, dict[str, Any]]] = []
    for coordinator in _business_coordinators(hass):
        # 条目级覆盖了 mode → 不参与全局合并（独立定时或实时）
        if _scheduled_override(coordinator.options) is not None:
            continue
        if not getattr(coordinator, "alert_pending", False):
            continue
        config = find_notification_config(hass, coordinator.options)
        if config is not None:
            pending.append((coordinator, config))
    if not pending:
        return

    title = await _integration_title(hass, pending[0][0].title)
    await _send_pending(
        hass, pending, title, f"{DOMAIN}_scheduled"
    )

async def async_flush_entry(hass: HomeAssistant, entry: Any) -> None:
    """条目级定时推送：单独推送一个条目（防御：残留调度被模式校验挡下）。"""
    data = getattr(entry, "runtime_data", None)
    coordinator = getattr(data, "coordinator", None)
    if coordinator is None or not getattr(coordinator, "alert_pending", False):
        return
    config = find_notification_config(hass, coordinator.options)
    if config is None or config.get(CONF_NOTIFY_MODE) != NOTIFY_MODE_SCHEDULED:
        return
    await _send_pending(
        hass,
        [(coordinator, config)],
        coordinator.title,
        f"{DOMAIN}_{sanitize_notification_id(coordinator.entry_id)}",
    )
