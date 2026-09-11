"""Binary sensors: is the agent there, is the browser up, is the panel sick."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .const import SETTING_KIOSK_MODE
from .coordinator import KioskPiCoordinator
from .entity import KioskPiEntity

# READ-ONLY PLATFORM. Every value comes off the coordinator's single poll, so
# there is nothing here for Home Assistant to serialise and a limit would only
# slow entity addition down.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KioskPiBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor and the reading behind it.

    ``value_fn`` may return None — "could not read" is not "off", and a sensor
    that reports off when it failed to look is the fail-permissive shape that
    makes a sick panel read healthy.
    """

    value_fn: Callable[[KioskPiCoordinator], bool | None]


def _throttled(coordinator: KioskPiCoordinator) -> bool | None:
    throttle = (coordinator.data or {}).get("throttle") or {}
    ok = throttle.get("ok")
    return None if ok is None else not ok


def _read_only_root(coordinator: KioskPiCoordinator) -> bool | None:
    mode = (coordinator.data or {}).get("rootFilesystem")
    return None if mode is None else mode != "rw"


def _cursor_hidden(coordinator: KioskPiCoordinator) -> bool | None:
    """Did the hide-cursor rule actually reach the live document.

    THE ONLY CURSOR READING THAT EXISTS OFF-DEVICE, and it is worth knowing
    exactly what it does and does not settle. `image.<panel>_screenshot` is a
    `Page.captureScreenshot`, taken out of Chromium's RENDERER compositor,
    while the stranded cursor is a `wl_pointer` surface Chromium hands to
    cage — so a screenshot shows no cursor whether or not one is on the glass,
    and a sweep of them once reported four walls clean with one of them stuck
    (jrackerby/kiosk-pi#9).

    UNAVAILABLE WHEN THE FEATURE IS OFF, rather than `off`. With `hideCursor`
    disabled a pointer is the CORRECT state for a bench host somebody is
    driving, and publishing that as a problem teaches an operator to ignore
    this entity on the hosts where it matters.

    ON means the extension loaded and its rule applied. It does NOT mean the
    glass is clean: a Chromium that already committed a cursor surface and
    never receives another pointer-enter can keep drawing it. That is the
    whole value — ON with a cursor still on the wall is the compositor-surface
    fault, OFF is a fix that never arrived, and before this the two were
    indistinguishable without standing in front of the panel.
    """
    data = coordinator.data or {}
    if not data.get("hideCursor"):
        return None
    style = data.get("cursorStyle")
    return None if style is None else style == "none"


SENSORS: tuple[KioskPiBinarySensorDescription, ...] = (
    KioskPiBinarySensorDescription(
        key="browser",
        translation_key="browser",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda c: (c.data or {}).get("browserRunning"),
    ),
    KioskPiBinarySensorDescription(
        key="kiosk_mode",
        translation_key="kiosk_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.setting(SETTING_KIOSK_MODE),
    ),
    KioskPiBinarySensorDescription(
        key="screensaver_active",
        translation_key="screensaver_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("screensaverOn"),
    ),
    # THROTTLING IS STICKY AND THAT IS WHY IT IS WORTH AN ENTITY. The firmware
    # keeps a since-boot half of the bitmask, so a wall that browned out at 3am
    # is still saying so at noon — which is the only way anybody finds out
    # about a failing power supply on a panel nobody is watching.
    KioskPiBinarySensorDescription(
        key="throttled",
        translation_key="throttled",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_throttled,
    ),
    # ext4 flips to read-only when an SD card wears out. Disk percentage does
    # not move, every other reading keeps answering, and the wall keeps
    # painting a stale page — this is the cheap signal that separates that from
    # a healthy panel.
    # NOT A SCREENSHOT QUESTION, which is why it is an entity. See
    # _cursor_hidden: the capture and the cursor live in different
    # compositors and never meet.
    KioskPiBinarySensorDescription(
        key="cursor_hidden",
        translation_key="cursor_hidden",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cursor_hidden,
    ),
    KioskPiBinarySensorDescription(
        key="filesystem_read_only",
        translation_key="filesystem_read_only",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_read_only_root,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        KioskPiBinarySensor(coordinator, description) for description in SENSORS
    ]
    entities.append(KioskPiAgentReachable(coordinator))
    async_add_entities(entities)


class KioskPiBinarySensor(KioskPiEntity, BinarySensorEntity):
    entity_description: KioskPiBinarySensorDescription

    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def available(self) -> bool:
        # An unreadable value is unavailable, not off. The description's
        # value_fn returns None for exactly this, and mapping it to False here
        # would throw away the distinction it was written to preserve.
        return super().available and self.is_on is not None


class KioskPiAgentReachable(KioskPiEntity, BinarySensorEntity):
    """Is the agent answering at all.

    THE ONE ENTITY THAT NEVER GOES UNAVAILABLE. Every other entity here is
    unavailable when the coordinator's refresh failed, which is correct — they
    describe a panel nobody can currently see. This one describes THE READING
    ITSELF, so a version of it that disappeared with its subject could never
    report the subject down, and the device page would go blank rather than
    say what happened.

    ``offline_expected`` is the one case where it does report unavailable, and
    deliberately: a panel deliberately switched off is not a connectivity
    fault, and publishing it as one trains an operator to ignore this entity.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "agent"

    def __init__(self, coordinator: KioskPiCoordinator) -> None:
        super().__init__(coordinator, "agent")

    @property
    def available(self) -> bool:
        return not (self.coordinator.data or {}).get("offlineExpected", False)

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "host": self.coordinator.client.host,
            "agent_version": data.get("agentVersion"),
            "browser_restart_count": data.get("browserRestartCount"),
            "browser_last_exit_code": data.get("browserLastExitCode"),
        }
