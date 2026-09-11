"""Two services, for the things no entity models cleanly.

``load_url`` IS THE TRANSIENT NAVIGATION and is deliberately not an entity.
Pointing a wall somewhere for thirty seconds — a camera feed, a doorbell, an
alert — is an action, not a state, and modelling it as a `text` entity would
make the panel's durable configuration read as the temporary thing on the
glass. ``select.<panel>_dashboard`` and ``text.<panel>_start_url`` are how a
caller says it means the change permanently.

``set_config`` IS THE ESCAPE HATCH for a setting this integration has not grown
an entity for. It is honest about being one: it takes the agent's own key names
and passes the value through the agent's typed coercion, so a wrong key is a
400 with the reason rather than a silently ignored write.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api import KioskPiError
from .const import DOMAIN
from .coordinator import KioskPiCoordinator

SERVICE_LOAD_URL = "load_url"
SERVICE_SET_CONFIG = "set_config"

LOAD_URL_SCHEMA = vol.Schema({
    vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Required("url"): cv.string,
})

SET_CONFIG_SCHEMA = vol.Schema({
    vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Required("key"): cv.string,
    vol.Required("value"): cv.string,
})


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[KioskPiCoordinator]:
    """Every targeted device's coordinator.

    A DEVICE THAT IS NOT ONE OF OURS IS AN ERROR, not a silent skip. A service
    call that quietly does nothing for three of four targets reports success
    and leaves the caller believing all four moved.
    """
    registry = dr.async_get(hass)
    found: list[KioskPiCoordinator] = []
    for device_id in call.data["device_id"]:
        device = registry.async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"unknown device: {device_id}")
        coordinator = next(
            (
                entry.runtime_data
                for entry_id in device.config_entries
                if (entry := hass.config_entries.async_get_entry(entry_id))
                and entry.domain == DOMAIN
                # A loaded entry is the one that has runtime_data. An entry
                # that failed to set up has none, and reaching for it would
                # raise an AttributeError the caller cannot act on.
                and getattr(entry, "runtime_data", None) is not None
            ),
            None,
        )
        if coordinator is None:
            raise ServiceValidationError(
                f"{device.name or device_id} is not a loaded Kiosk Pi panel"
            )
        found.append(coordinator)
    return found


async def async_setup_services(hass: HomeAssistant) -> None:
    async def load_url(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            try:
                await coordinator.async_apply(
                    coordinator.client.load_url(call.data["url"])
                )
            except KioskPiError as err:
                raise HomeAssistantError(str(err)) from err

    async def set_config(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            try:
                await coordinator.async_apply(
                    coordinator.client.set_string_setting(
                        call.data["key"], call.data["value"]
                    )
                )
            except KioskPiError as err:
                # The agent's own message names the key and why it was
                # refused; passing it through beats replacing it with a
                # generic failure the caller then has to go and look up.
                raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, SERVICE_LOAD_URL, load_url,
                                 schema=LOAD_URL_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_CONFIG, set_config,
                                 schema=SET_CONFIG_SCHEMA)
