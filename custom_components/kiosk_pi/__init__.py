"""The Kiosk Pi integration.

Drives a Raspberry Pi wall panel through ``pikioskd`` — an agent that runs on
the panel, owns the browser, and answers a Fully-Kiosk-shaped HTTP admin API.
The agent ships in this same repository under ``agent/``; the two halves are
versioned together and the README says how to install it.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
)

from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
)
from .coordinator import KioskPiCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NOTIFY,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

type KioskPiConfigEntry = ConfigEntry[KioskPiCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once, not once per entry.

    Registering from async_setup_entry would re-register on every panel
    added and, worse, tear the services down when the FIRST panel is
    removed — leaving the remaining panels with services that no longer
    exist and automations that fail with a service-not-found nobody
    connects to the removal.
    """
    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: KioskPiConfigEntry) -> bool:
    # A PANEL MIGRATED FROM THE SSH TRANSPORT HAS NO PASSWORD YET. Raising here
    # rather than letting the coordinator fail its first poll starts the reauth
    # flow immediately and does not depend on the agent being installed and
    # answering — which, before that password exists, it usually is not.
    if not entry.data.get(CONF_PASSWORD):
        raise ConfigEntryAuthFailed(
            f"{entry.title} has no agent password yet. Run agent/install.sh on "
            f"the panel and enter the password it prints."
        )

    coordinator = KioskPiCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KioskPiConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: KioskPiConfigEntry) -> None:
    """Options changed. ``offline_expected`` is one of them and it changes what
    the coordinator does at all, so a reload is the cheapest way to make a new
    disposition take effect everywhere at once."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """2 -> 3 (1.0.0): the transport changes, and the credential is not ours.

    Versions below 1.0.0 drove a panel over SSH with a key and a username.
    1.0.0 drives an agent over HTTP with a password generated ON THE DEVICE by
    the agent's installer, so migration genuinely cannot compute the new
    configuration: the credential does not exist until somebody has run
    install.sh on that panel.

    THIS USED TO RETURN False, AND THAT MADE THE UPGRADE IMPOSSIBLE TO PERFORM.
    `migration_error` is a NON-RECOVERABLE config entry state: nothing moves an
    entry out of it in a running instance — async_unload refuses it outright, so
    async_reload and async_setup both raise OperationNotAllowed — and the entry
    keeps the panel's unique id while it sits there, so the "add the panel
    again" the failure told the operator to perform aborted as
    `already_configured`. The only route through was deleting the entry, which
    is what returning False was trying to avoid, because deleting it takes the
    panel's recorded history with it. Measured on the first panel, 2026-09-10.

    So migrate the SHAPE here, which is computable, and let Home Assistant's own
    reauth flow collect the one thing that is not. The entry lands in a
    recoverable state, keeps its entry_id and therefore its registry rows and
    history, and the operator gets the standard "reconfigure this" prompt once
    the agent is installed.
    """
    if entry.version < 3:
        data = dict(entry.data)
        # The SSH transport's keys go; they name a credential path and a daemon
        # port that 1.0.0 does not use, and leaving them beside the new keys
        # reads like configuration rather than residue.
        for dead in ("ssh_user", "ssh_key", "glances_port", "hostname"):
            data.pop(dead, None)
        data.setdefault(CONF_PORT, DEFAULT_PORT)
        data.setdefault(CONF_SSL, False)
        data.setdefault(CONF_VERIFY_SSL, True)
        # NO PASSWORD, deliberately. async_setup_entry turns its absence into
        # ConfigEntryAuthFailed, which is what starts the reauth flow.
        data.pop(CONF_PASSWORD, None)
        # MINOR 1, NOT 2, AND THE DIFFERENCE IS THE WHOLE BUG. Landing a v2
        # entry directly on 3.2 makes the `minor_version < 2` step below a
        # no-op for it, so the ssh-era rows that step exists to adopt are
        # skipped on exactly the entries that have them. It read correct
        # because the panel it was written against was already at 3.1 — the
        # one entry in the fleet for which the step did run.
        hass.config_entries.async_update_entry(
            entry, data=data, version=3, minor_version=1
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"agent_migration_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="agent_migration",
            translation_placeholders={"title": entry.title},
            learn_more_url="https://github.com/jrackerby/kiosk-pi#upgrading-to-100",
        )
        _LOGGER.warning(
            "%s was configured for the SSH transport, which 1.0.0 replaces. "
            "Install the pikioskd agent on the panel (see agent/install.sh), "
            "then enter the password it prints when Home Assistant asks to "
            "reconfigure this panel. The entry and its history are kept.",
            entry.title,
        )

    # 3 RATHER THAN 2, TO REACH THE ENTRIES 3.2 ALREADY PASSED OVER.
    #
    # The sequencing bug fixed alongside this landed most existing panels on
    # 3.2 without the adoption ever running — the step was a no-op for them,
    # and correcting the step could not reach them afterwards because they
    # already carried its version. A migration step is only ever
    # run once per entry, so a step that shipped broken cannot be repaired in
    # place: it has to be re-offered under a NEW number. The work is
    # idempotent (a row already re-keyed is simply not found), so entries that
    # did get it lose nothing by being asked again.
    if entry.version == 3 and entry.minor_version < 3:
        _adopt_ssh_era_entity_rows(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=3)

    return True


# The ssh-era entity keys that name THE SAME MEASUREMENT as a 1.0.0 entity.
# Deliberately short: a row remapped onto a key that means something slightly
# different silently attaches one entity's recorded history to another
# reading, which is worse than leaving an orphan behind. Only exact matches
# are here.
#
# THE TEST IS THE KEY, NOT WHETHER THE ENTITY IDS COLLIDE. This map was first
# written for the collision case — an old row holding the id a new entity
# wants, which pushes the new one to `_2` — but that is only the LOUD half.
# `wifi_signal` is the quiet half and was missed by the narrower rule: the
# 1.0.0 sensor is named "Wi-Fi signal" where the ssh-era one was "WiFi
# signal", so the slugs differ, nothing collides, no `_2` appears and nothing
# looks wrong — while `sensor.<host>_wifi_signal` is abandoned to a row
# nothing updates and its replacement quietly turns up at
# `sensor.<host>_wi_fi_signal`. Every dashboard and automation naming the old
# id breaks with no `_2` to hint at why. Measured on the first panel: the ssh-era
# row is ours (platform kiosk_pi, same entry), still ssh-keyed, and reading
# `unavailable`.
#
# Re-keying it makes the 1.0.0 sensor adopt that row, so the entity KEEPS the
# familiar `_wifi_signal` id along with its history and no second id is
# minted.
_SSH_ERA_UNIQUE_IDS: dict[str, str] = {
    "dashboard": "dashboard",
    "resolution": "resolution",
    "wifi_signal": "wifi_signal",
}


@callback
def _adopt_ssh_era_entity_rows(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Re-key the pre-1.0.0 registry rows the new entities would collide with.

    THE ENTITY ID IS THE CONTRACT, and the registry never frees one. An ssh-era
    row keyed `<hostname>_resolution` keeps `sensor.<host>_resolution` for ever,
    so the 1.0.0 sensor — keyed on the entry id instead — registers beside it as
    `sensor.<host>_resolution_2` and every dashboard and automation still
    pointing at the original reads a row nothing updates any more. Measured on
    the first panel, 2026-09-10: `select.<host>_dashboard` and
    `sensor.<host>_resolution` both went dead this way.

    Re-keying the OLD row to the new unique id makes the new entity adopt it:
    same entity_id, same history, no `_2`, and nothing orphaned.

    Ids taken by ANOTHER integration are not touched and cannot be — on this
    the existing `sensor.<host>_ip_address` belongs to cyber_estate and
    `sensor.<host>_uptime` to linux_monitor. Those `_2` suffixes are a
    cross-integration fact, not this migration's to fix.
    """
    registry = er.async_get(hass)

    for old_key, new_key in _SSH_ERA_UNIQUE_IDS.items():
        new_unique = f"{entry.entry_id}_{new_key}"

        # FOUND BY SUFFIX ON THIS ENTRY'S OWN ROWS, NOT BY REBUILDING THE
        # HOSTNAME. The ssh-era ids are `<hostname>_<key>`, and the obvious
        # way to find one is to reconstruct that prefix — but the only sources
        # left for it are `entry.data["hostname"]`, which the 2->3 step
        # deliberately drops, and `entry.title`, which an operator can rename
        # in the UI at any time. A renamed entry then matched nothing and
        # stranded every row in silence, with no `_2` and no error: the same
        # shape as the two misses already fixed here. Measured with a renamed
        # entry, 2026-09-10.
        #
        # Scoped to this config entry, a row ending `_<old_key>` and NOT
        # carrying the entry-id prefix can only be one of ours from the ssh
        # era, so the suffix is exact without needing to know the hostname at
        # all.
        old_entity = _ssh_era_row(registry, entry, old_key)
        if old_entity is None:
            continue

        # A row already carrying the new key is one the platforms created on a
        # previous start — it holds nothing the old row does not, so it goes
        # first, or the re-key below collides with it.
        twin = _find(registry, entry, new_unique)
        if twin is not None:
            registry.async_remove(twin.entity_id)

        registry.async_update_entity(
            old_entity.entity_id, new_unique_id=new_unique
        )
        _LOGGER.info(
            "%s: kept %s and its history for the 1.0.0 entity",
            entry.title, old_entity.entity_id,
        )


def _find(registry, entry: ConfigEntry, unique_id: str):
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        if row.unique_id == unique_id:
            return row
    return None


def _ssh_era_row(registry, entry: ConfigEntry, old_key: str):
    """This entry's pre-1.0.0 row for ``old_key``, found without its hostname.

    Rows minted by 1.0.0 all carry the entry id as their prefix, so excluding
    that prefix leaves only ssh-era rows — and within one config entry those
    are ours by definition.
    """
    prefix = f"{entry.entry_id}_"
    suffix = f"_{old_key}"
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        if row.unique_id.startswith(prefix):
            continue
        if row.unique_id.endswith(suffix):
            return row
    return None
