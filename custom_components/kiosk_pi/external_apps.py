"""The external dashboard app list — persisted, and editable from the UI.

SEEDED ONCE, NEVER RE-SEEDED. The first read finds no Store and seeds it from
the shipped defaults; every read after that comes from the Store alone.
Re-seeding on an empty list would silently resurrect an app somebody removed,
which is indistinguishable from the removal never having taken.

THE SHIPPED DEFAULT IS EMPTY, and that is deliberate. A board's address is
deployment inventory: a real one committed here would be wrong for every other
installation and would publish a private hostname from a public repository. The
list is built by the operator through the options flow.

KEYED BY ``key``, LAST WRITE WINS. Adding a key that exists is an EDIT, not a
duplicate — two apps at one key would leave the picker and the reachability
probe disagreeing about which URL that key means.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_EXTERNAL_DASHBOARD_APPS,
    EXTERNAL_APPS_STORE_KEY,
    EXTERNAL_APPS_STORE_VERSION,
)


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, EXTERNAL_APPS_STORE_VERSION, EXTERNAL_APPS_STORE_KEY)


def _clean(raw: Any) -> list[dict[str, Any]]:
    """Drop anything that cannot be used.

    A record with no key cannot be offered and one with no URL cannot be
    loaded, so both are dropped rather than carried — an option that selects
    to nothing is worse than an absent one.
    """
    if not isinstance(raw, list):
        return []
    return [app for app in raw
            if isinstance(app, dict) and app.get("key") and app.get("url")]


async def async_get_apps(hass: HomeAssistant) -> list[dict[str, Any]]:
    store = _store(hass)
    stored = await store.async_load()
    if stored is None:
        seeded = [dict(app) for app in DEFAULT_EXTERNAL_DASHBOARD_APPS]
        await store.async_save(seeded)
        return seeded
    return _clean(stored)


async def async_add_app(hass: HomeAssistant, key: str, url: str,
                        surface: str) -> None:
    apps = [app for app in await async_get_apps(hass) if app.get("key") != key]
    apps.append({"key": key, "url": url, "surface": surface})
    await _store(hass).async_save(apps)


async def async_remove_app(hass: HomeAssistant, key: str) -> None:
    apps = [app for app in await async_get_apps(hass) if app.get("key") != key]
    await _store(hass).async_save(apps)
