"""Sensors: what the panel is showing, and the few host readings that explain it.

WHAT IS DELIBERATELY ABSENT. apt state, kernel versions, pending updates, NIC
inventory as entities — ``linux_monitor`` owns generic OS health for every host
in this estate, and each of these panels already has an entry from it. A second
copy here would put two integrations on one device page reporting one fact from
two transports, which is how a host grows two health sensors that disagree.

What is here earns its place by answering a question about the PANEL. Memory and
temperature explain a browser that keeps dying; the link readings explain a
board that loads slowly; the rest describe the surface itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from homeassistant.util import dt as dt_util

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


def _uptime(coordinator: KioskPiCoordinator) -> datetime | None:
    """The boot time, as a timestamp — not a counter.

    A seconds-since-boot sensor writes a new state on every single poll, which
    fills the recorder for a value nothing reads at that resolution. The boot
    INSTANT is constant between reboots, so it writes once per reboot and the
    frontend renders the elapsed time from it.
    """
    seconds = (coordinator.data or {}).get("uptimeSeconds")
    if seconds is None:
        return None
    return dt_util.utcnow() - timedelta(seconds=float(seconds))


def _primary_ip(coordinator: KioskPiCoordinator) -> str | None:
    """The address on the interface that is actually up.

    NO INTERFACE NAME IS ASSUMED. This fleet is on wifi today with eth0 down,
    and a hardcoded ``wlan0`` is the same class of mistake as a hardcoded
    hostname table: correct until somebody plugs in a cable.
    """
    for nic in (coordinator.data or {}).get("interfaces") or []:
        if isinstance(nic, dict) and nic.get("ipv4") and nic.get("state") == "up":
            return str(nic["ipv4"])
    return None


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
    KioskPiSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_primary_ip,
        attrs_fn=lambda c: {"interfaces": (c.data or {}).get("interfaces") or []},
    ),
    KioskPiSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_uptime,
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
