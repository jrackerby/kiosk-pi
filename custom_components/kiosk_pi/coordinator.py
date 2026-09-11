"""One coordinator per panel: one HTTP round trip, one picture of the wall.

ONE POLL, NOT SIX. ``deviceInfo`` returns the whole state of the panel in a
single call, so every entity on the device page is reading the same instant.
A coordinator that fetched the screen, the browser and the settings separately
would report three independent failure modes and would produce a torn read
whenever the wall changed mid-poll — the select showing the new board while the
current-page sensor still showed the old one, with nothing to say which was
right.

SETTINGS ARE FETCHED ALONGSIDE, and that is a second call, deliberately.
``deviceInfo`` carries the values a *reading* needs; the settings map carries
everything a *control* needs to render its own current position — the two
timers, both brightnesses, the error URL. Folding them into one response would
mean the agent serving its whole configuration on the polling path, which is a
larger payload every 30 seconds for values that change when somebody changes
them. The two are gathered concurrently and treated as one refresh: if either
fails the refresh fails, because half a picture is the torn read again.

THIS COORDINATOR RAISES ``UpdateFailed`` AND ITS ENTITIES GO UNAVAILABLE. That
is the quality scale's ``entity-unavailable`` rule and it governs here: this is
a coordinator reading a DEVICE, which can be unreachable, so an entity that
kept publishing its last value would be asserting something about a panel
nobody can see. The never-raise contract applies to a coordinator
reading other entities, which has no device to lose. ONE entity opts out —
``binary_sensor.<panel>_agent`` overrides ``available`` and stays up — because
a monitor that disappears with its subject cannot report the subject down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    KioskPiAuthError,
    KioskPiClient,
    KioskPiConnectionError,
    KioskPiError,
)
from .const import (
    CONF_HOST,
    CONF_OFFLINE_EXPECTED,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
    CURRENT_PAGE_DWELL,
    DEFAULT_PORT,
    DOMAIN,
    REQUEST_TIMEOUT,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class KioskPiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """The panel's state, refreshed on a fixed interval."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        data = entry.data
        self.client = KioskPiClient(
            async_get_clientsession(hass),
            host=data[CONF_HOST],
            password=data[CONF_PASSWORD],
            port=data.get(CONF_PORT, DEFAULT_PORT),
            use_ssl=data.get(CONF_SSL, False),
            verify_ssl=data.get(CONF_VERIFY_SSL, True),
            timeout=REQUEST_TIMEOUT,
        )
        self.settings: dict[str, Any] = {}

        # The dwell's state. Held on the coordinator rather than on the sensor
        # so it survives an entity being reloaded, and so two consumers of the
        # same reading cannot dwell differently.
        self._last_page: str | None = None
        self._last_page_at: datetime | None = None

        # A once-only INFO, deduped on a STABLE CONDITION TOKEN rather than on
        # the message. A message carrying the error text turns one condition
        # into a new log line every time the wording moves — a DNS failure and
        # a refused connection are the same condition to an operator, and both
        # are "the panel is unreachable".
        self._last_condition: str | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {data[CONF_HOST]}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )

    # --- disposition --------------------------------------------------------

    @property
    def offline_expected(self) -> bool:
        """A panel that is deliberately off.

        Not an error and not a fault: a wall unplugged for the summer should
        stop filling the log and stop showing as a problem, without its entry
        being deleted and its history with it.
        """
        return bool(self.entry.options.get(CONF_OFFLINE_EXPECTED, False))

    @property
    def panel_name(self) -> str:
        """The panel's own name for itself, falling back to its entry title.

        ASK THE HOST, NEVER THE TABLE, where the host can answer. A name typed
        once at config time and frozen from then on is how a fleet ends up with
        entities named for machines that answer to something else.
        """
        data = self.data or {}
        return str(data.get("deviceName") or data.get("hostname")
                   or self.entry.title)

    # --- the refresh --------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if self.offline_expected:
            # Not a fetch and not a failure. The entities read unavailable via
            # their own `available`, and nothing is dialled — inventing a
            # reading for a host we did not contact is the fail-permissive
            # shape this integration exists to remove.
            return {"offlineExpected": True}

        try:
            info, settings = await asyncio.gather(
                self.client.device_info(),
                self.client.list_settings(),
            )
        except KioskPiAuthError as err:
            # Re-auth, not a retry. A wrong password will still be wrong in 30
            # seconds, and retrying it forever is how an agent's log fills with
            # 401s that nobody connects to a Home Assistant entry.
            raise ConfigEntryAuthFailed(str(err)) from err
        except KioskPiConnectionError as err:
            self._log_condition("unreachable", "cannot reach %s: %s",
                                self.client.host, err)
            raise UpdateFailed(str(err)) from err
        except KioskPiError as err:
            self._log_condition("command_failed", "%s answered an error: %s",
                                self.client.host, err)
            raise UpdateFailed(str(err)) from err

        self._log_recovery()
        self.settings = {k: v for k, v in settings.items() if k != "status"}
        data = {k: v for k, v in info.items() if k != "status"}
        data["offlineExpected"] = False
        data["currentURL"] = self._dwell_current_page(data.get("currentURL"))
        return data

    def _dwell_current_page(self, live: Any) -> str | None:
        """Hold the last known page across a restart-shaped gap.

        FALL DWELL, NEVER RISE DWELL. A page appearing is published at once;
        only its disappearance waits, because a browser restart takes the tab
        away and brings it back and publishing None in between makes every
        deliberate restart read as an outage. LOSING SIGHT OF THE SOURCE COUNTS
        AS A FALL: an unreadable page and an absent one are the same fall here,
        and both are dwelt on rather than one being treated as evidence.

        It never falls back to the configured start URL. A cross-check that
        mirrors the thing it checks asserts nothing at all — the whole value of
        this reading is that it can disagree with ``startURL``.
        """
        now = dt_util.utcnow()
        if live:
            self._last_page = str(live)
            self._last_page_at = now
            return self._last_page
        if self._last_page_at is None:
            return None
        if now - self._last_page_at <= CURRENT_PAGE_DWELL:
            return self._last_page
        self._last_page = None
        return None

    # --- logging ------------------------------------------------------------

    def _log_condition(self, token: str, fmt: str, *args: Any) -> None:
        """INFO once at the crossing, and nothing until the condition changes.

        Deduped on ``token``, a stable name for the condition, never on the
        rendered message: a count or an error string in the compared value
        turns one condition into a new line on every poll.

        INFO, NOT WARNING, and the split is on who acts. A panel that is
        unreachable is this integration reporting on its own subject and needs
        no edit from anybody — the entity going unavailable is the report. A
        warning is for something nobody can wait out, like a credential that
        will never work again.
        """
        if self._last_condition == token:
            return
        self._last_condition = token
        _LOGGER.info(fmt, *args)

    def _log_recovery(self) -> None:
        if self._last_condition is not None:
            _LOGGER.info("%s is answering again", self.client.host)
            self._last_condition = None

    # --- helpers for the platforms -----------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    async def async_apply(self, coro: Any) -> None:
        """Run a write and refresh, so the entity's next state is a READ-BACK.

        A command returning OK only says the call was dispatched. Refreshing
        here means every control's state comes from the panel rather than from
        an optimistic local assumption — which is the difference between a
        switch that reports what the wall is doing and one that reports what it
        was asked to do.
        """
        await coro
        await self.async_request_refresh()
