"""tests_ha 共享真环境辅助（pytest 不采集本文件）。"""

from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall
from consumable_manager.const import DOMAIN

def check(desc: str, cond: bool) -> None:
    """离线 harness 的 check(desc, cond) 断言 → pytest assert。"""
    assert cond, desc

def clean_persist(hass) -> None:
    """清掉共享 testing_config 下本集成的持久文件与进程内缓存残留。"""
    base = Path(hass.config.config_dir)
    for rel in (
        ".consumable_manager/user_library.json",
        ".storage/consumable_manager.bindings",
    ):
        (base / rel).unlink(missing_ok=True)
    from consumable_manager import bindings as _b
    key = _b._cache_key(hass)
    _b._BINDINGS_CACHE.pop(key, None)
    _b._PRIMED.discard(key)
    _b._STORES.pop(key, None)

def make_entry(entry_id: str, title: str, data: dict, options: dict | None = None):
    """协调器测试用最小 ConfigEntry 替身（真实 DataUpdateCoordinator 仅存引用）。"""
    entry = SimpleNamespace(
        entry_id=entry_id,
        title=title,
        data=dict(data),
        options=dict(options or {}),
        runtime_data=None,
        state=ConfigEntryState.NOT_LOADED,
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

def _stub_update_entry(entry, **kwargs) -> None:
    """同步直写 entry 属性的 async_update_entry 替身（真实方法为 async）。"""
    for key, value in kwargs.items():
        setattr(entry, key, value)

def inject_entries(monkeypatch, hass, entries) -> None:
    """monkeypatch config_entries：注入 mock 条目 + 同步直写 options。"""
    by_id = {e.entry_id: e for e in entries}
    def _stub_get_entry(entry_id, default=None):
        return by_id.get(entry_id, default)
    def _stub_get_known_entry(entry_id):
        entry = by_id.get(entry_id)
        if entry is None:
            from homeassistant.config_entries import UnknownEntry
            raise UnknownEntry(entry_id)
        return entry
    monkeypatch.setattr(
        hass.config_entries, "async_update_entry", _stub_update_entry)
    monkeypatch.setattr(
        hass.config_entries, "async_entries",
        lambda domain=None: list(entries)
        if domain in (None, DOMAIN) else [],
    )
    monkeypatch.setattr(
        hass.config_entries, "async_get_entry", _stub_get_entry)
    monkeypatch.setattr(
        hass.config_entries, "async_get_known_entry", _stub_get_known_entry)

class _SpyServices:
    """hass.services 替身：async_call / async_register 记录；其余转发真实 registry。"""
    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: list[dict[str, Any]] = []
        self.registered: dict[str, Any] = {}
        self.handlers: dict[str, Any] = {}
    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        context: Any = None,
        target: dict[str, Any] | None = None,
        return_response: bool = False,
    ) -> Any:
        self.calls.append({
            "domain": domain,
            "service": service,
            "data": service_data or {},
            "target": target,
        })
        return None
    def async_register(
        self,
        domain: str,
        service: str,
        service_func: Any,
        schema: Any = None,
        supports_response: Any = None,
        job_type: Any = None,
        **kwargs: Any,
    ) -> None:
        self.registered[service] = supports_response
        self.handlers[service] = service_func
    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

def install_service_spy(monkeypatch, hass) -> list:
    """记录型 hass.services 替身（通知发送断言）。"""
    spy = _SpyServices(hass.services)
    monkeypatch.setattr(hass, "services", spy)
    return spy.calls

def install_register_spy(monkeypatch, hass):
    """记录型注册断言替身：返回 (registered, handlers)。"""
    spy = _SpyServices(hass.services)
    monkeypatch.setattr(hass, "services", spy)
    return spy.registered, spy.handlers

def make_call(hass, service: str, data: dict | None = None) -> ServiceCall:
    """构造真实 ServiceCall（签名 (hass, domain, service, data)）。"""
    return ServiceCall(hass, DOMAIN, service, data=data)
