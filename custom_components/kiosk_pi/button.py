"""Buttons: the one-shot actions on a panel."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .api import KioskPiClient
from .coordinator import KioskPiCoordinator
from .entity import KioskPiBrowserEntity, KioskPiEntity

# THIS PLATFORM WRITES TO THE DEVICE, so its calls are serialised. The agent is
# a single-threaded settings map behind an HTTP server on a Raspberry Pi: two
# concurrent writes to the same settings file are a lost update with no
# conflict marker and no diff to find it by, and two concurrent browser
# commands race for one DevTools session.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class KioskPiButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[KioskPiClient], Coroutine[Any, Any, Any]]
    # True where the action goes through DevTools and therefore cannot work
    # with the browser down. Those buttons are greyed rather than offered and
    # failed — a control that is present and cannot work teaches an operator
    # that a red toast is normal.
    needs_browser: bool = True


BUTTONS: tuple[KioskPiButtonDescription, ...] = (
    KioskPiButtonDescription(
        key="load_start_url",
        translation_key="load_start_url",
        press_fn=lambda client: client.load_start_url(),
    ),
    KioskPiButtonDescription(
        key="restart_browser",
        translation_key="restart_browser",
        # NOT needs_browser. Restarting a browser that is down is the correct
        # thing to press when it is down, and the agent owns the process rather
        # than reaching it through DevTools.
        needs_browser=False,
        press_fn=lambda client: client.restart_browser(),
    ),
    KioskPiButtonDescription(
        key="clear_cache",
        translation_key="clear_cache",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.clear_cache(),
    ),
    KioskPiButtonDescription(
        key="clear_cookies",
        translation_key="clear_cookies",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda client: client.clear_cookies(),
    ),
    KioskPiButtonDescription(
        key="to_foreground",
        translation_key="to_foreground",
        press_fn=lambda client: client.to_foreground(),
    ),
    KioskPiButtonDescription(
        key="restart_device",
        translation_key="restart_device",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        needs_browser=False,
        press_fn=lambda client: client.reboot(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        (KioskPiBrowserButton if description.needs_browser else KioskPiButton)(
            coordinator, description
        )
        for description in BUTTONS
    )


class _PressMixin:
    entity_description: KioskPiButtonDescription
    coordinator: KioskPiCoordinator

    async def async_press(self) -> None:
        # Refresh after the press so the entities that describe the result —
        # current page, browser running, restart count — come from a READ of
        # the panel rather than from an assumption about what the press did.
        await self.coordinator.async_apply(
            self.entity_description.press_fn(self.coordinator.client)
        )


class KioskPiButton(_PressMixin, KioskPiEntity, ButtonEntity):
    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description


class KioskPiBrowserButton(_PressMixin, KioskPiBrowserEntity, ButtonEntity):
    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
