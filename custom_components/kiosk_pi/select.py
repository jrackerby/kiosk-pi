"""The dashboard picker: which external board this wall is pointed at.

EXTERNAL WEB APPS ONLY. This estate's architecture is external, API-driven
dashboards rather than native Lovelace YAML boards, and the Lovelace board
fleet this picker's ancestor enumerated is decommissioned. The previous version
walked every Lovelace view in the instance, classified each one by transcribing
a card's own surface ladder, and offered the non-control ones; all of that
machinery is deleted rather than ported, because it now enumerates a set that
is empty by construction and reads as a picker that has stopped working.

SELECTING WRITES THE START URL, NOT A TRANSIENT NAVIGATION. Pointing a wall at
a board is a re-provisioning: it must survive a reboot, a browser restart and an
outage. ``loadURL`` alone would move the glass and leave the panel coming back
to its old board the next time anything restarted it — which is discovered days
later, by which point nobody connects the two. The agent is therefore told the
new start URL and then told to load it, in that order, so a failure between the
two leaves the durable configuration correct.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .const import CONF_ALLOW_CONTROL_BOARDS, SETTING_START_URL
from .coordinator import KioskPiCoordinator
from .discovery import async_refresh_cache, cached_apps
from .entity import KioskPiEntity
from .surface import allowed_apps, out_of_policy, surface_of

# THIS PLATFORM WRITES TO THE DEVICE, so its calls are serialised. The agent is
# a single-threaded settings map behind an HTTP server on a Raspberry Pi: two
# concurrent writes to the same settings file are a lost update with no
# conflict marker and no diff to find it by, and two concurrent browser
# commands race for one DevTools session.
PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_refresh_cache(hass)
    async_add_entities([KioskPiDashboardSelect(hass, entry.runtime_data)])


class KioskPiDashboardSelect(KioskPiEntity, SelectEntity):
    _attr_translation_key = "dashboard"

    def __init__(self, hass: HomeAssistant,
                 coordinator: KioskPiCoordinator) -> None:
        super().__init__(coordinator, "dashboard")
        self.hass = hass

    # --- the one accessor ---------------------------------------------------

    @property
    def _allow_control(self) -> bool:
        return bool(self.coordinator.entry.options.get(
            CONF_ALLOW_CONTROL_BOARDS, False
        ))

    @property
    def _current_url(self) -> str | None:
        """WHAT THE PANEL IS CONFIGURED TO COME BACK TO, not what it is painting.

        A select's position is a setting, so it reads the setting. Reading the
        live page instead would make the entity flip to `unknown` every time
        the wall showed an outage page or an automation flashed a camera feed
        at it — a picker whose position changes without anybody picking is a
        picker nobody trusts. The live page has its own sensor, and the two
        disagreeing is a finding rather than a defect.
        """
        return self.coordinator.setting(SETTING_START_URL)

    def _permitted(self) -> list[dict[str, Any]]:
        return allowed_apps(cached_apps(self.hass), self._current_url,
                            self._allow_control)

    # --- the entity ---------------------------------------------------------

    @property
    def options(self) -> list[str]:
        return sorted({str(app["key"]) for app in self._permitted()})

    @property
    def current_option(self) -> str | None:
        """The key of the app whose URL the panel starts on, or None.

        None where the panel is on something this integration did not assign —
        a hand-set URL, an outage page, a board that has been removed from the
        list. That is honest: the picker has no position because the wall is
        not on any of its options, and inventing one would hide it.
        """
        current = self._current_url
        if not current:
            return None
        for app in cached_apps(self.hass):
            if app.get("url") == current:
                return str(app["key"])
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        current = self._current_url
        return {
            "start_url": current,
            "surface": next(
                (surface_of(app) for app in cached_apps(self.hass)
                 if app.get("url") == current),
                None,
            ),
            # Says WHY a board is offered that policy would otherwise filter:
            # the wall is already on it, so the position is shown rather than
            # hidden behind an `unknown` that looks like a dead pointer.
            "target_out_of_policy": out_of_policy(
                cached_apps(self.hass), current, self._allow_control
            ),
            "control_boards_allowed": self._allow_control,
        }

    async def async_select_option(self, option: str) -> None:
        # RE-PROBED BEFORE THE WRITE, not merely at the last refresh. A cache
        # is a claim with a timestamp, and the gap between rendering a picker
        # and somebody choosing from it is unbounded.
        await async_refresh_cache(self.hass)
        permitted = self._permitted()
        chosen = next((app for app in permitted if str(app["key"]) == option), None)
        if chosen is None:
            # THE SAME ACCESSOR AS `options`. A guard on the option list alone
            # would leave every filtered board reachable from more-info, an
            # automation and voice, which is the whole reason the filter is in
            # the integration rather than in a card.
            raise ValueError(
                f"{option} is not an available board for this panel. "
                f"Available: {', '.join(sorted(str(a['key']) for a in permitted))}"
            )
        url = str(chosen["url"])
        client = self.coordinator.client
        # Durable first, then the glass. A failure between the two leaves the
        # panel configured correctly and merely still showing the old board,
        # which the next restart fixes by itself.
        await client.set_string_setting(SETTING_START_URL, url)
        await client.load_url(url)
        await self.coordinator.async_request_refresh()
