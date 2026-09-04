"""实体↔耗材 绑定映射。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import LOGGER

STORAGE_KEY = "consumable_manager.bindings"
STORAGE_VERSION = 1

# 按 hass config 路径分键，避免多实例（如测试）串味
_STORES: dict[str, Store] = {}
_BINDINGS_CACHE: dict[str, dict[str, str]] = {}
# 读-改-写互斥锁（每 hass 一把）：并发 bind/unbind 不丢写
_LOCKS: dict[str, asyncio.Lock] = {}
# 已异步预热过的 hass（见 async_prime）：预热后同步读只走内存，不碰磁盘
_PRIMED: set[str] = set()

def _cache_key(hass: HomeAssistant) -> str:
    """缓存分键：用 config 路径（含 Store key）保证多 hass 实例隔离。"""
    return hass.config.path(STORAGE_KEY)

def _lock(hass: HomeAssistant) -> asyncio.Lock:
    """取得（或惰性创建）当前 hass 的写锁。"""
    key = _cache_key(hass)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock

def _store(hass: HomeAssistant) -> Store:
    """取得（或惰性创建）当前 hass 的 Store 实例。"""
    key = _cache_key(hass)
    st = _STORES.get(key)
    if st is None:
        st = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        _STORES[key] = st
    return st

def _parse_raw(raw: object) -> dict[str, str]:
    """把 Store 包装格式（``{"version":..,"data":..}``）或裸 dict 归一化。"""
    if isinstance(raw, dict) and "data" in raw:
        payload = raw["data"]
    else:
        payload = raw
    if isinstance(payload, dict):
        return {str(k): str(v) for k, v in payload.items() if v}
    return {}

def _read_raw_sync(hass: HomeAssistant) -> dict[str, str]:
    """同步读取 ``.storage`` 下的绑定文件（兼容 Store 的包装格式）。"""
    path = Path(_store(hass).path)
    if not path.is_file():
        return {}
    try:
        return _parse_raw(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as err:
        LOGGER.warning("读取绑定文件失败，视为空：%s", err)
        return {}

def _cache_get(hass: HomeAssistant) -> dict[str, str]:
    """取得（或惰性加载）当前 hass 的绑定缓存。"""
    key = _cache_key(hass)
    cached = _BINDINGS_CACHE.get(key)
    if cached is None:
        cached = _read_raw_sync(hass)
        _BINDINGS_CACHE[key] = cached
    return cached

def get_binding(hass: HomeAssistant, entity_id: str) -> str | None:
    """同步读取某实体的绑定耗材 id（带按 config 路径的进程内缓存）。"""
    return _cache_get(hass).get(entity_id)

def get_all_bindings(hass: HomeAssistant) -> dict[str, str]:
    """同步读取全部绑定（带缓存）。"""
    return _cache_get(hass)

async def async_load_bindings(hass: HomeAssistant) -> dict[str, str]:
    """异步经 Store 重载并刷新缓存，返回当前全部绑定。"""
    raw = await _store(hass).async_load()
    data = _parse_raw(raw)
    _BINDINGS_CACHE[_cache_key(hass)] = data
    _PRIMED.add(_cache_key(hass))
    return data

async def async_prime(hass: HomeAssistant) -> None:
    """异步预热绑定缓存（每个 hass 仅一次）。"""
    key = _cache_key(hass)
    if key in _PRIMED:
        return
    await async_load_bindings(hass)

def is_primed(hass: HomeAssistant) -> bool:
    """当前 hass 的绑定缓存是否已完成异步预热。"""
    return _cache_key(hass) in _PRIMED

async def _async_current(hass: HomeAssistant) -> dict[str, str]:
    """取当前全部绑定（已预热走内存，否则异步加载；全程不阻塞事件循环）。"""
    key = _cache_key(hass)
    cached = _BINDINGS_CACHE.get(key)
    if key in _PRIMED and cached is not None:
        return cached
    return await async_load_bindings(hass)

async def async_set_binding(
    hass: HomeAssistant, entity_id: str, consumable_id: str
) -> None:
    """建立/更新一条实体↔耗材绑定（经 Store 原子持久化，锁内读-改-写）。"""
    async with _lock(hass):
        data = dict(await _async_current(hass))
        data[entity_id] = consumable_id
        _BINDINGS_CACHE[_cache_key(hass)] = data
        await _store(hass).async_save(data)

async def async_remove_binding(hass: HomeAssistant, entity_id: str) -> bool:
    """删除一条绑定；返回是否真的删除了（锁内读-改-写）。"""
    async with _lock(hass):
        data = dict(await _async_current(hass))
        if entity_id in data:
            del data[entity_id]
            _BINDINGS_CACHE[_cache_key(hass)] = data
            await _store(hass).async_save(data)
            return True
        return False