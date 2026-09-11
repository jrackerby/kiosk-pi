"""Which external dashboard apps are reachable right now.

ONE PROBE PER APP PER REFRESH, AND REACHABILITY IS MEASURED RATHER THAN
ASSUMED. An option in a picker is a promise that selecting it will put
something on the glass; offering a board whose server is down converts a
visible outage into a wall that silently goes blank when somebody chooses it.

THE PROBE RUNS FROM HOME ASSISTANT, WHICH IS NOT WHERE THE BROWSER RUNS, and
that is a real limitation stated rather than hidden: Home Assistant's own
container may resolve a name the panel resolves fine, or the reverse. A probe
failure therefore drops an app from the OPTIONS but never repoints a wall, and
the current target is kept regardless — the wall's own reading is the authority
on what it can reach.

CACHED, WITH THE CACHE OWNED HERE. The select entity reads this; the options
flow refreshes it after an edit. Keeping the cache in a platform module is what
previously made a config-flow module import a platform module to trigger a
refresh, so the data layer lives on its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import APP_PROBE_TIMEOUT, DOMAIN
from .external_apps import async_get_apps

_LOGGER = logging.getLogger(__name__)

_CACHE_KEY = f"{DOMAIN}_app_cache"


async def _probe(session: aiohttp.ClientSession, url: str) -> bool:
    """Does this board answer.

    ANY HTTP RESPONSE COUNTS, NOT ONLY 200. A board behind an auth redirect
    answers 302, and one whose index is generated answers 204 on a HEAD — both
    are servers that are up. The question here is whether something is
    listening and serving that path at all, and narrowing it to 200 silently
    drops every board that redirects.
    """
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=APP_PROBE_TIMEOUT),
            allow_redirects=False,
        ) as response:
            return response.status < 500
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.debug("app probe failed for %s: %s", url, err)
        return False


async def async_refresh_cache(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Probe every configured app concurrently; cache and return the live ones."""
    apps = await async_get_apps(hass)
    if not apps:
        hass.data[_CACHE_KEY] = []
        return []

    session = async_get_clientsession(hass)
    # Concurrent: N apps at four seconds each, serialised, would put the whole
    # picker behind the slowest board every time an entity refreshed it.
    results = await asyncio.gather(
        *(_probe(session, str(app["url"])) for app in apps)
    )
    live = [app for app, reachable in zip(apps, results, strict=True) if reachable]
    hass.data[_CACHE_KEY] = live
    return live


def cached_apps(hass: HomeAssistant) -> list[dict[str, Any]]:
    """What the last probe found. Empty before the first one has run.

    EMPTY IS NOT "NOTHING IS REACHABLE" — it is also "nothing has been probed
    yet". The select treats both the same way (no options to offer) and keeps
    the wall's current target regardless, so the ambiguity cannot repoint
    anything.
    """
    return list(hass.data.get(_CACHE_KEY) or [])
