"""Sensors: what the panel is showing, and the few host readings that explain it.

WHAT IS DELIBERATELY ABSENT. apt state, kernel versions, pending updates, NIC
inventory as entities — AND THE HOST'S IP ADDRESS AND UPTIME — ``linux_monitor``
and ``cyber_estate`` own generic OS health for every host in this estate, and
each of these panels already has an entry from both. A second copy here would
put two integrations on one device page reporting one fact from two transports,
which is how a host grows two health sensors that disagree.

THAT IS NOT A THEORY: 1.0.0 shipped ``ip_address`` and ``uptime`` anyway and
every one of them landed as a ``_2`` beside the owner's — except on one panel,
where kiosk_pi won the race and took the canonical ``sensor.<host>_ip_address``
while ``cyber_estate``'s became the ``_2``. The registry never frees an id, so
the collision is permanent in whichever direction it happened to fall. Disabling
the losers in the registry is undone by the next reinstall; not registering them
is not (jrackerby/HA#467, jrackerby/kiosk-pi#6).

What is here earns its place by answering a question about the PANEL. Memory and
temperature explain a browser that keeps dying; the link readings explain a
board that loads slowly; the rest describe the surface itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfInformation,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KioskPiConfigEntry
from .coordinator import KioskPiCoordinator
from .entity import KioskPiEntity

# READ-ONLY PLATFORM. Every value comes off the coordinator's single poll, so
# there is nothing here for Home Assistant to serialise and a limit would only
# slow entity addition down.
PARALLEL_UPDATES = 0

# A URL is far longer than the 255 characters Home Assistant permits in a state.
# An over-long state is REJECTED by the state machine, so the entity would read
# `unknown` on exactly the panels whose address is most worth knowing — a board
# URL with a query string is routinely over 200. The full value therefore lives
# in an attribute and the state carries a truncated form that says it is one.
STATE_MAX = 255
_ELLIPSIS = "…"


def _truncate(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= STATE_MAX:
        return value
    return value[: STATE_MAX - 1] + _ELLIPSIS


@dataclass(frozen=True, kw_only=True)
class KioskPiSensorDescription(SensorEntityDescription):
    value_fn: Callable[[KioskPiCoordinator], Any]
    attrs_fn: Callable[[KioskPiCoordinator], dict[str, Any]] | None = None


def _monitor(coordinator: KioskPiCoordinator) -> str | None:
    """The screen's own identity, from its EDID.

    MAKE AND MODEL ARE JOINED HERE, NOT AT THE AGENT. ``wlr-randr`` reports
    them as two EDID fields and the agent publishes them as two, because one of
    them is routinely absent and a pre-joined string cannot say which half is
    missing. A monitor reporting only ``Make:`` is still identified; a state
    reading "ASUS None" identifies nothing.
    """
    data = coordinator.data or {}
    parts = [str(p) for p in (data.get("displayMake"), data.get("displayModel"))
             if p]
    return " ".join(parts) or None


SENSORS: tuple[KioskPiSensorDescription, ...] = (
    KioskPiSensorDescription(
        key="current_page",
        translation_key="current_page",
        value_fn=lambda c: _truncate((c.data or {}).get("currentURL")),
        attrs_fn=lambda c: {"url": (c.data or {}).get("currentURL")},
    ),
    KioskPiSensorDescription(
        key="agent_version",
        translation_key="agent_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("agentVersion"),
    ),
    KioskPiSensorDescription(
        key="browser_version",
        translation_key="browser_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("browserVersion"),
    ),
    # THE COUNT IS THE SIGNAL, NOT THE STATE. A crash-looping browser under a
    # supervisor that restarts it always reads "running" on every poll that
    # happens to land between crashes; the restart count is the only reading
    # that moves.
    KioskPiSensorDescription(
        key="browser_restarts",
        translation_key="browser_restarts",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("browserRestartCount"),
    ),
    KioskPiSensorDescription(
        key="cpu_temperature",
        translation_key="cpu_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("cpuTemperatureC"),
    ),
    KioskPiSensorDescription(
        key="free_memory",
        translation_key="free_memory",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("availableMemoryMB"),
    ),
    KioskPiSensorDescription(
        key="memory_used",
        translation_key="memory_used",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("memoryUsedPercent"),
    ),
    KioskPiSensorDescription(
        key="free_storage",
        translation_key="free_storage",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("freeStorageMB"),
    ),
    KioskPiSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: ((c.data or {}).get("wifi") or {}).get("rssi"),
        attrs_fn=lambda c: {
            "ssid": ((c.data or {}).get("wifi") or {}).get("ssid"),
            "interface": ((c.data or {}).get("wifi") or {}).get("interface"),
            "link_quality": ((c.data or {}).get("wifi") or {}).get("linkQuality"),
        },
    ),
    KioskPiSensorDescription(
        key="resolution",
        translation_key="resolution",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data or {}).get("resolution"),
        attrs_fn=lambda c: {
            "output": (c.data or {}).get("displayOutput"),
            "model": (c.data or {}).get("displayModel"),
            "orientation": (c.data or {}).get("orientation"),
            "instrument": (c.data or {}).get("screenInstrument"),
        },
    ),
    # THE TWO READINGS THE RETIRED SSH LADDER CARRIED (jrackerby/kiosk-pi#7).
    # Both were readable off `command_line` sensors until that ladder was
    # retired, and 1.0.0 published neither: the SSID only as an attribute of
    # the signal sensor, where nothing can template over the fleet, and the
    # make not at all. An attribute is not readable off the registry, which is
    # the whole of what a glass inventory is.
    KioskPiSensorDescription(
        key="ssid",
        translation_key="ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: ((c.data or {}).get("wifi") or {}).get("ssid"),
        attrs_fn=lambda c: {
            "interface": ((c.data or {}).get("wifi") or {}).get("interface"),
        },
    ),
    KioskPiSensorDescription(
        key="monitor",
        translation_key="monitor",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_monitor,
        attrs_fn=lambda c: {
            "make": (c.data or {}).get("displayMake"),
            "model": (c.data or {}).get("displayModel"),
            "output": (c.data or {}).get("displayOutput"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        KioskPiSensor(coordinator, description) for description in SENSORS
    )


class KioskPiSensor(KioskPiEntity, SensorEntity):
    entity_description: KioskPiSensorDescription

    def __init__(self, coordinator: KioskPiCoordinator,
                 description: KioskPiSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)
