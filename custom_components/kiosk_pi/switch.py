"""Switches: the screen, the screensaver, and the two locks."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .api import KioskPiClient
from .const import SETTING_KIOSK_MODE, SETTING_MAINTENANCE_MODE
from .coordinator import KioskPiCoordinator
from .entity import KioskPiEntity

# THIS PLATFORM WRITES TO THE DEVICE, so its calls are serialised. The agent is
# a single-threaded settings map behind an HTTP server on a Raspberry Pi: two
# concurrent writes to the same settings file are a lost update with no
# conflict marker and no diff to find it by, and two concurrent browser
# commands race for one DevTools session.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class KioskPiSwitchDescription(SwitchEntityDescription):
    is_on_fn: Callable[[KioskPiCoordinator], bool | None]
    set_fn: Callable[[KioskPiClient, bool], Coroutine[Any, Any, Any]]


SWITCHES: tuple[KioskPiSwitchDescription, ...] = (
    KioskPiSwitchDescription(
        key="screen",
        translation_key="screen",
        device_class=SwitchDeviceClass.SWITCH,
        is_on_fn=lambda c: (c.data or {}).get("screenOn"),
        set_fn=lambda client, on: (
            client.screen_on() if on else client.screen_off()
        ),
    ),
    KioskPiSwitchDescription(
        key="screensaver",
        translation_key="screensaver",
        is_on_fn=lambda c: (c.data or {}).get("screensaverOn"),
        set_fn=lambda client, on: (
            client.start_screensaver() if on else client.stop_screensaver()
        ),
    ),
    # KIOSK LOCK IS A SECURITY BOUNDARY, NOT A PREFERENCE. With it off the
    # browser is an ordinary window: navigation chrome, a URL bar and every
    # other page on the network are one keystroke away on any panel that ever
    # gets a keyboard plugged into it.
    KioskPiSwitchDescription(
        key="kiosk_lock",
        translation_key="kiosk_lock",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda c: c.setting(SETTING_KIOSK_MODE),
        set_fn=lambda client, on: client.set_bool_setting(SETTING_KIOSK_MODE, on),
    ),
    KioskPiSwitchDescription(
        key="maintenance_mode",
        translation_key="maintenance_mode",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda c: c.setting(SETTING_MAINTENANCE_MODE),
        set_fn=lambda client, on: client.set_bool_setting(
            SETTING_MAINTENANCE_MODE, on
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        KioskPiSwitch(coordinator, description) for description in SWITCHES
    )


class KioskPiSwitch(KioskPiEntity, SwitchEntity):
    entity_description: KioskPiSwitchDescription

    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiSwitchDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator)

    @property
    def available(self) -> bool:
        # NO OPTIMISTIC STATE. A screen whose power could not be read has an
        # unknown position, and a switch that renders it as off is asserting
        # something nobody measured — the display module returns None for
        # exactly this case rather than guessing.
        return super().available and self.is_on is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_apply(
            self.entity_description.set_fn(self.coordinator.client, on)
        )
