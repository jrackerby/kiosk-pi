"""Notify: the full-screen overlay message.

THE PAGE UNDERNEATH IS UNTOUCHED. The overlay is drawn into the live document,
so the board keeps its socket, its timers and its state and clearing the message
reveals a live board rather than one that has to reload. Navigating to a notice
page and back would cost the board's entire session, which a directive surface
cannot afford at the moment it is needed.

SENDING AN EMPTY MESSAGE CLEARS IT. There is no second entity for that: an
overlay with no text is an overlay that is not there, and a separate "clear"
button would be a second control for one state.
"""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .coordinator import KioskPiCoordinator
from .entity import KioskPiBrowserEntity

# THIS PLATFORM WRITES TO THE DEVICE, so its calls are serialised. The agent is
# a single-threaded settings map behind an HTTP server on a Raspberry Pi: two
# concurrent writes to the same settings file are a lost update with no
# conflict marker and no diff to find it by, and two concurrent browser
# commands race for one DevTools session.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([KioskPiOverlay(entry.runtime_data)])


class KioskPiOverlay(KioskPiBrowserEntity, NotifyEntity):
    _attr_translation_key = "overlay_message"
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, coordinator: KioskPiCoordinator) -> None:
        super().__init__(coordinator, "overlay_message")

    async def async_send_message(self, message: str,
                                 title: str | None = None) -> None:
        # Title and message are joined rather than rendered separately: the
        # overlay is one block of centred text on a wall read from across a
        # room, and a second type size in it buys nothing at that distance.
        text = f"{title}\n{message}" if title else message
        await self.coordinator.async_apply(
            self.coordinator.client.set_overlay(text)
        )
