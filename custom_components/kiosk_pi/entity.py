"""The shared entity base. DeviceInfo is defined once, here."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import KioskPiCoordinator


class KioskPiEntity(CoordinatorEntity[KioskPiCoordinator]):
    """Base entity for everything this integration publishes.

    ``_attr_has_entity_name`` is what keeps the panel's address out of the
    entity id: the id becomes ``<device slug>_<entity slug>``, so a host that
    moves address does not mean renaming every entity on it.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: KioskPiCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        # THE UNIQUE ID IS THE ENTRY ID, NOT THE HOSTNAME. The old integration
        # keyed on the hostname, which meant a panel renamed on the host — or
        # reported under a different case — minted a whole second set of
        # entities beside the originals, with the first set left behind
        # holding the history. The entry id is stable for the life of the
        # entry and is not a fact about the device.
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def device_info(self) -> DeviceInfo:
        data = self._data
        # MAC CONNECTIONS MERGE THIS DEVICE WITH THE ONE THE NETWORK
        # INTEGRATION ALREADY HAS. Declaring a (mac, ...) connection folds this
        # device into any other integration's device carrying the same MAC, so
        # the device tracker, linux_monitor and these kiosk entities land on ONE
        # device page. That is the point of declaring them.
        #
        # A MERGE IS NOT CLEANLY REVERSIBLE, so the MACs come off the machine —
        # the agent reads /sys/class/net — and never off a label in another
        # system. Trusting a label has merged a device into the wrong physical
        # machine before.
        #
        # ALL real NICs are declared, not just the one carrying the address we
        # dialled: a panel that gets cabled later should join its own device
        # rather than spawn a second one.
        connections = {
            (CONNECTION_NETWORK_MAC, format_mac(nic["mac"]))
            for nic in (data.get("interfaces") or [])
            if isinstance(nic, dict) and nic.get("mac")
        }
        version = data.get("agentVersion")
        return DeviceInfo(
            connections=connections,
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name=self.coordinator.panel_name,
            manufacturer="Raspberry Pi",
            model=data.get("model"),
            sw_version=f"pikioskd {version}" if version else None,
            configuration_url=self.coordinator.client.base_url,
        )

    @property
    def available(self) -> bool:
        if self._data.get("offlineExpected"):
            return False
        return super().available


class KioskPiBrowserEntity(KioskPiEntity):
    """An entity whose subject is the BROWSER, not the panel.

    A screenshot, a navigation and a cache flush all fail when the agent is up
    and Chromium is not, and publishing them as available in that state means
    an operator presses a button that cannot work and gets a red toast instead
    of a greyed control. Everything that goes through DevTools inherits this.
    """

    @property
    def available(self) -> bool:
        return super().available and bool(self._data.get("browserRunning"))
