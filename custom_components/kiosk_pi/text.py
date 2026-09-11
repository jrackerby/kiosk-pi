"""Text: the start URL, and the outage page.

WHY THESE ARE TEXT ENTITIES AND NOT SENSORS. Both are settings an operator
changes, and a sensor is read-only, so a sensor here would mean the only way to
repoint a panel permanently is a service call somebody has to remember. They
are also both long values that must be settable to EMPTY — the outage page is
disabled by clearing it — which a select cannot express and a number cannot
hold.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.text import TextEntity, TextEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .api import KioskPiClient
from .const import SETTING_ERROR_URL, SETTING_START_URL
from .coordinator import KioskPiCoordinator
from .entity import KioskPiEntity

# THIS PLATFORM WRITES TO THE DEVICE, so its calls are serialised. The agent is
# a single-threaded settings map behind an HTTP server on a Raspberry Pi: two
# concurrent writes to the same settings file are a lost update with no
# conflict marker and no diff to find it by, and two concurrent browser
# commands race for one DevTools session.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class KioskPiTextDescription(TextEntityDescription):
    value_fn: Callable[[KioskPiCoordinator], str | None]
    set_fn: Callable[[KioskPiClient, str], Coroutine[Any, Any, Any]]


TEXTS: tuple[KioskPiTextDescription, ...] = (
    KioskPiTextDescription(
        key="start_url",
        translation_key="start_url",
        entity_category=EntityCategory.CONFIG,
        # A URL is comfortably longer than the platform's 100-character
        # default, and a board address with a query string reaches several
        # hundred. Truncation here would silently write a broken URL.
        native_max=1024,
        value_fn=lambda c: c.setting(SETTING_START_URL),
        set_fn=lambda client, value: client.set_string_setting(
            SETTING_START_URL, value
        ),
    ),
    KioskPiTextDescription(
        key="error_url",
        translation_key="error_url",
        entity_category=EntityCategory.CONFIG,
        native_min=0,
        native_max=1024,
        value_fn=lambda c: c.setting(SETTING_ERROR_URL),
        set_fn=lambda client, value: client.set_string_setting(
            SETTING_ERROR_URL, value
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
        KioskPiText(coordinator, description) for description in TEXTS
    )


class KioskPiText(KioskPiEntity, TextEntity):
    entity_description: KioskPiTextDescription

    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiTextDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | None:
        return self.entity_description.value_fn(self.coordinator)

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_apply(
            self.entity_description.set_fn(self.coordinator.client, value)
        )
