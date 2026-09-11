"""Diagnostics for one panel.

THE PASSWORD IS REDACTED, and so is the agent's own copy of it in the settings
map. A diagnostics dump is the artefact an operator attaches to a public issue,
so anything that would give somebody remote control of a wall has to be gone
before it is written rather than remembered about afterwards.

THE SSID IS NOT REDACTED. It is not a credential, it is the single most useful
field for diagnosing a panel that roams, and redacting facts that are merely
identifying makes a dump that cannot answer the question it was collected for.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import KioskPiConfigEntry
from .const import CONF_PASSWORD

TO_REDACT = {CONF_PASSWORD, "remoteAdminPassword"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KioskPiConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "last_update_success": coordinator.last_update_success,
        "device_info": async_redact_data(coordinator.data or {}, TO_REDACT),
        "settings": async_redact_data(coordinator.settings, TO_REDACT),
    }
