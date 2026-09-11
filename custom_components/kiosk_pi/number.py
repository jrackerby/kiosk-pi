"""Numbers: brightness and the two idle timers.

BOTH TIMERS PERMIT ZERO AND ZERO MEANS NEVER — the agent's semantics and
Fully's. A minimum of 1 here would make "never" unreachable from the UI and
would leave a panel configured for never showing a value it cannot be set back
to, which reads as a broken entity rather than as a range that excludes it.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .api import KioskPiClient
from .const import (
    BRIGHTNESS_MAX,
    SETTING_SCREEN_OFF_TIMER,
    SETTING_SCREENSAVER_BRIGHTNESS,
    SETTING_SCREENSAVER_TIMER,
    TIMER_MAX_SECONDS,
)
from .coordinator import KioskPiCoordinator
from .entity import KioskPiEntity

# THIS PLATFORM WRITES TO THE DEVICE, so its calls are serialised. The agent is
# a single-threaded settings map behind an HTTP server on a Raspberry Pi: two
# concurrent writes to the same settings file are a lost update with no
# conflict marker and no diff to find it by, and two concurrent browser
# commands race for one DevTools session.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class KioskPiNumberDescription(NumberEntityDescription):
    value_fn: Callable[[KioskPiCoordinator], float | None]
    set_fn: Callable[[KioskPiClient, int], Coroutine[Any, Any, Any]]
    # Brightness has no software control on a panel driving an HDMI monitor —
    # there is no backlight device to write. The entity is published and
    # reports unavailable rather than being hidden, because "this panel cannot
    # do this" is a fact worth seeing on the device page.
    requires_backlight: bool = False


NUMBERS: tuple[KioskPiNumberDescription, ...] = (
    KioskPiNumberDescription(
        key="screen_brightness",
        translation_key="screen_brightness",
        native_min_value=0,
        native_max_value=BRIGHTNESS_MAX,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        requires_backlight=True,
        value_fn=lambda c: (c.data or {}).get("screenBrightness"),
        set_fn=lambda client, value: client.set_brightness(value),
    ),
    KioskPiNumberDescription(
        key="screensaver_brightness",
        translation_key="screensaver_brightness",
        native_min_value=0,
        native_max_value=BRIGHTNESS_MAX,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        requires_backlight=True,
        value_fn=lambda c: c.setting(SETTING_SCREENSAVER_BRIGHTNESS),
        set_fn=lambda client, value: client.set_int_setting(
            SETTING_SCREENSAVER_BRIGHTNESS, value
        ),
    ),
    KioskPiNumberDescription(
        key="screen_off_timer",
        translation_key="screen_off_timer",
        native_min_value=0,
        native_max_value=TIMER_MAX_SECONDS,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: c.setting(SETTING_SCREEN_OFF_TIMER),
        set_fn=lambda client, value: client.set_int_setting(
            SETTING_SCREEN_OFF_TIMER, value
        ),
    ),
    KioskPiNumberDescription(
        key="screensaver_timer",
        translation_key="screensaver_timer",
        native_min_value=0,
        native_max_value=TIMER_MAX_SECONDS,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: c.setting(SETTING_SCREENSAVER_TIMER),
        set_fn=lambda client, value: client.set_int_setting(
            SETTING_SCREENSAVER_TIMER, value
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
        KioskPiNumber(coordinator, description) for description in NUMBERS
    )


class KioskPiNumber(KioskPiEntity, NumberEntity):
    entity_description: KioskPiNumberDescription

    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiNumberDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        value = self.entity_description.value_fn(self.coordinator)
        return None if value is None else float(value)

    @property
    def available(self) -> bool:
        if not super().available or self.native_value is None:
            return False
        if self.entity_description.requires_backlight:
            # Measured, not assumed: the agent reports the backlight's own
            # maximum, and None means there is no /sys/class/backlight device.
            return (self._data.get("screenBrightnessMax") or 0) > 0
        return True

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_apply(
            self.entity_description.set_fn(self.coordinator.client, int(value))
        )
