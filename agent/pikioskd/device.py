"""Host telemetry — the ``deviceInfo`` payload.

WHAT IS DELIBERATELY NOT HERE: apt state, kernel upgrade status, dmesg counts,
unattended-upgrade configuration. Generic OS health belongs to ``linux_monitor``
across every host you run, kiosks included, and duplicating it here would
put two integrations on one device page reporting the same fact from two
transports — which is how a host ends up with two health sensors that disagree
and a second entity id carrying a ``_2`` suffix nobody can remove.

What IS here is what is specific to a *kiosk*: the screen, the browser, the
surface being shown, and the few host readings that explain a wall behaving
badly (temperature and throttling, memory, storage, link quality). Each one
earns its place by answering a question about the panel rather than about the
computer.

EVERY READING IS OPTIONAL AND ABSENCE IS REPORTED AS ABSENCE. A key whose
source could not be read is ``None``, never zero and never a stale carry —
``ok at zero`` and ``could not read`` do not collapse, and a temperature that
reads 0 because the thermal zone moved is indistinguishable from a cold room.
"""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The Raspberry Pi firmware's throttle bitmask. The low half is "right now",
# the high half is "since boot" and is sticky — a wall that browned out at
# 3am still reports it at noon, which is the whole point of reading it.
THROTTLE_BITS: dict[int, str] = {
    0: "under_voltage",
    1: "arm_frequency_capped",
    2: "currently_throttled",
    3: "soft_temperature_limit",
    16: "under_voltage_occurred",
    17: "arm_frequency_capped_occurred",
    18: "throttling_occurred",
    19: "soft_temperature_limit_occurred",
}

_WIRELESS_RE = re.compile(
    r"^\s*(?P<iface>[^:]+):\s+\S+\s+(?P<quality>-?[\d.]+)\s+(?P<signal>-?[\d.]+)"
)


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _run(argv: list[str], timeout: int = 5) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def uptime_seconds() -> float | None:
    raw = _read_text("/proc/uptime")
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (IndexError, ValueError):
        return None


def load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except OSError:
        return None


def memory() -> dict[str, float | None]:
    """Totals in MiB, plus a used percentage.

    MemAvailable, NOT MemFree. MemFree excludes the page cache, so a healthy
    Linux box reports single-digit free memory forever and every threshold
    built on it fires permanently. MemAvailable is the kernel's own estimate of
    what a new allocation could actually get, which is the question being asked.
    """
    raw = _read_text("/proc/meminfo")
    if not raw:
        return {"totalMB": None, "availableMB": None, "usedPercent": None}
    values: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        number = parts[1].strip().split()
        if number and number[0].isdigit():
            values[parts[0]] = int(number[0])  # kB
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return {"totalMB": None, "availableMB": None, "usedPercent": None}
    return {
        "totalMB": round(total / 1024, 1),
        "availableMB": round(available / 1024, 1),
        "usedPercent": round((total - available) / total * 100, 1),
    }


def storage(path: str = "/") -> dict[str, float | None]:
    try:
        stats = os.statvfs(path)
    except OSError:
        return {"totalMB": None, "freeMB": None, "usedPercent": None}
    total = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    if total <= 0:
        return {"totalMB": None, "freeMB": None, "usedPercent": None}
    return {
        "totalMB": round(total / 1024 / 1024, 1),
        "freeMB": round(free / 1024 / 1024, 1),
        "usedPercent": round((total - free) / total * 100, 1),
    }


def root_filesystem_mode() -> str | None:
    """``rw`` or ``ro``.

    ext4 flips to read-only when an SD card wears out. Disk percentage does not
    move, every other reading keeps answering, and the wall keeps painting a
    stale page — this is the one cheap signal that separates that from a
    healthy panel.
    """
    out = _run(["findmnt", "-no", "OPTIONS", "/"])
    if not out:
        return None
    return out.strip().split(",")[0] or None


def cpu_temperature_c() -> float | None:
    """Degrees Celsius from the thermal zone, or None.

    THE ZONE IS FOUND, NOT ASSUMED. ``thermal_zone0`` is the CPU on a Pi and is
    something else on plenty of other ARM boards, so the type file is read and
    matched. A wrong zone reports a real number off the wrong sensor, which is
    the failure mode that survives review.
    """
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        kind = (_read_text(os.path.join(zone, "type")) or "").strip()
        if kind not in ("cpu-thermal", "cpu_thermal", "soc_thermal", "x86_pkg_temp"):
            continue
        raw = _read_text(os.path.join(zone, "temp"))
        if raw is None:
            continue
        try:
            return round(int(raw.strip()) / 1000, 1)
        except ValueError:
            continue
    return None


def decode_throttle(raw: str | None) -> dict[str, Any]:
    """``vcgencmd get_throttled``'s bitmask, named.

    An empty or unparseable reading yields ``{"ok": None}`` — unknown, not
    clean. A throttle report defaulting to healthy is precisely the reading a
    browning-out panel would give.
    """
    if not raw:
        return {"ok": None, "flags": [], "raw": None}
    text = raw.strip().replace("throttled=", "")
    try:
        value = int(text, 16)
    except ValueError:
        return {"ok": None, "flags": [], "raw": text}
    flags = [name for bit, name in THROTTLE_BITS.items() if value & (1 << bit)]
    return {"ok": value == 0, "flags": sorted(flags), "raw": text}


def throttle() -> dict[str, Any]:
    if shutil.which("vcgencmd") is None:
        return {"ok": None, "flags": [], "raw": None}
    return decode_throttle(_run(["vcgencmd", "get_throttled"]))


def cpu_frequency_mhz() -> float | None:
    """The running clock of the first CPU, from cpufreq, in MHz.

    THE COMPANION TO THE THROTTLE BITMASK. ``arm_frequency_capped`` says the
    firmware has pulled the clock down; this says by how much, and it moves
    before the bit does on a panel drifting towards its thermal limit. Read
    from the kernel's cpufreq rather than ``vcgencmd measure_clock`` so it is
    also right on a host that is not a Raspberry Pi.
    """
    raw = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if raw is None:
        return None
    try:
        return round(int(raw.strip()) / 1000, 0)
    except ValueError:
        return None


_VOLTS_RE = re.compile(r"volt=(?P<volts>[\d.]+)V")


def parse_core_voltage(raw: str | None) -> float | None:
    """``vcgencmd measure_volts core`` — ``volt=0.9360V`` — as a float."""
    if not raw:
        return None
    match = _VOLTS_RE.search(raw)
    if not match:
        return None
    try:
        return float(match.group("volts"))
    except ValueError:
        return None


def core_voltage_v() -> float | None:
    """The SoC core rail, from the firmware, or None where there is no firmware.

    Not the 5V input — nothing on a Pi can read that — but the rail the
    firmware lowers when it throttles, so a wall reporting ``under_voltage``
    shows it here as a number that moved rather than a bit that flipped.
    """
    if shutil.which("vcgencmd") is None:
        return None
    return parse_core_voltage(_run(["vcgencmd", "measure_volts", "core"]))


# DRM connectors, from sysfs. The compositor is the authority on what it is
# DRIVING; the kernel is the authority on what is PLUGGED IN, and the two
# disagree in exactly the case worth catching: with nothing connected, cage
# starts, Chromium starts, the process table is full, `browserRunning` is
# true — and DevTools never opens, so every browser-backed reading is unknown
# on a panel that reads healthy (jrackerby/HA#771). The `Writeback` connector
# is the compositor's own virtual output and is never a screen.
_DRM_GLOB = "/sys/class/drm/card*-*/status"


def parse_connector_name(path: str) -> str | None:
    """``/sys/class/drm/card0-HDMI-A-1/status`` -> ``HDMI-A-1``.

    Matches the name ``wlr-randr`` uses for the same output, so the two
    instruments' readings join on it.
    """
    directory = os.path.basename(os.path.dirname(path))
    _card, sep, name = directory.partition("-")
    return name if sep and name else None


def drm_connectors(pattern: str = _DRM_GLOB) -> list[dict[str, Any]] | None:
    """Every physical connector and its hotplug status, or None if unreadable.

    ``status`` is what the kernel reads back off the connector's hotplug
    detect line: ``connected``, ``disconnected`` or ``unknown``. An empty
    list is a host with a DRM device and no connectors; None is a host with
    no readable DRM at all — the two are different findings.
    """
    paths = sorted(glob.glob(pattern))
    if not paths:
        return None
    records: list[dict[str, Any]] = []
    for path in paths:
        name = parse_connector_name(path)
        if not name or name.startswith("Writeback"):
            continue
        status = (_read_text(path) or "").strip() or None
        records.append({"name": name, "status": status})
    return records


def display_connected(connectors: list[dict[str, Any]] | None) -> bool | None:
    """Is anything plugged in. True, False, or None for could-not-read.

    UNKNOWN IS NOT DISCONNECTED. A connector whose status could not be read
    is left out of the decision; if that leaves nothing decidable the answer
    is None, never False — a restart decision made on an unreadable sysfs
    would restart the browser on every host that lacks the file.
    """
    if connectors is None:
        return None
    known = [c["status"] for c in connectors if c.get("status") in
             ("connected", "disconnected")]
    if not known:
        return None
    return "connected" in known


def parse_wireless(raw: str | None) -> dict[str, Any]:
    """``/proc/net/wireless`` — the kernel file, not ``iw``.

    ``iw`` is not installed on the fleet's images and adding it means adding a
    package to provisioning; the kernel file is always present when a driver is
    bound, needs no privileges and no package. ``nmcli`` was the other
    candidate and reports a 0-100 quality PERCENTAGE, which is a different
    quantity from dBm and is derived from it — reporting one under the other's
    name is a unit error that nothing downstream can detect.

    Layout: two header lines, then one row per interface. Both numeric columns
    carry a trailing period the kernel prints.
    """
    if not raw:
        return {"interface": None, "rssi": None, "linkQuality": None}
    for line in raw.splitlines()[2:]:
        match = _WIRELESS_RE.match(line)
        if not match:
            continue
        try:
            return {
                "interface": match.group("iface").strip(),
                "linkQuality": int(float(match.group("quality"))),
                "rssi": int(float(match.group("signal"))),
            }
        except ValueError:
            continue
    return {"interface": None, "rssi": None, "linkQuality": None}


def wifi() -> dict[str, Any]:
    record = parse_wireless(_read_text("/proc/net/wireless"))
    record["ssid"] = None
    out = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if out:
        for line in out.splitlines():
            if line.startswith("yes:"):
                record["ssid"] = line.split(":", 1)[1] or None
                break
    return record


def interfaces() -> list[dict[str, Any]]:
    """Every REAL nic — the device symlink test drops ``lo`` and virtuals.

    No interface name is assumed anywhere. This fleet is on wifi today with
    ``eth0`` down, and a hardcoded ``wlan0`` is the same class of mistake as a
    hardcoded hostname table: correct until somebody plugs in a cable.
    """
    records: list[dict[str, Any]] = []
    for address_path in sorted(glob.glob("/sys/class/net/*/address")):
        directory = os.path.dirname(address_path)
        name = os.path.basename(directory)
        if name == "lo" or not os.path.exists(os.path.join(directory, "device")):
            continue
        mac = (_read_text(address_path) or "").strip() or None
        state = (_read_text(os.path.join(directory, "operstate")) or "").strip() or None
        ipv4 = None
        out = _run(["ip", "-4", "-o", "addr", "show", "dev", name])
        if out:
            parts = out.split()
            if len(parts) > 3:
                ipv4 = parts[3].split("/")[0]
        records.append({"name": name, "mac": mac, "ipv4": ipv4, "state": state})
    return records


def chromium_version(binary: str = "chromium") -> str | None:
    out = _run([binary, "--version"], timeout=15)
    if not out:
        return None
    return out.strip().split(" built on ")[0].removeprefix("Chromium ").strip() or None


def model() -> str | None:
    """The board's own name, from device-tree. NUL-terminated by the kernel."""
    raw = _read_text("/proc/device-tree/model")
    if raw is None:
        return None
    return raw.replace("\x00", "").strip() or None


def base_info() -> dict[str, Any]:
    """The host half of ``deviceInfo`` — everything not owned by the browser."""
    memory_record = memory()
    storage_record = storage()
    return {
        "hostname": socket.gethostname(),
        "model": model(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "uptimeSeconds": uptime_seconds(),
        "bootTime": (time.time() - (uptime_seconds() or 0)) or None,
        "loadAverage": load_average(),
        "totalMemoryMB": memory_record["totalMB"],
        "availableMemoryMB": memory_record["availableMB"],
        "memoryUsedPercent": memory_record["usedPercent"],
        "totalStorageMB": storage_record["totalMB"],
        "freeStorageMB": storage_record["freeMB"],
        "storageUsedPercent": storage_record["usedPercent"],
        "rootFilesystem": root_filesystem_mode(),
        "cpuTemperatureC": cpu_temperature_c(),
        "cpuFrequencyMHz": cpu_frequency_mhz(),
        "coreVoltageV": core_voltage_v(),
        "throttle": throttle(),
        "wifi": wifi(),
        "interfaces": interfaces(),
    }
