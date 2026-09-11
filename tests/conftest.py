"""Fixtures for the integration suite.

THE AGENT IS MOCKED AT THE HTTP BOUNDARY, not at the client. Mocking
``KioskPiClient`` would test the coordinator against a stub whose shape nobody
checks, and would pass for ever after the agent's replies changed. Mocking the
socket means the URL, the Authorization header, the status codes and the JSON
envelope are all exercised — which is where this integration's real contract
with the agent lives.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.kiosk_pi.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)

HOST = "192.0.2.10"
PORT = 2323
PASSWORD = "not-a-real-secret"
BASE = f"http://{HOST}:{PORT}/"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Without this the harness refuses to load anything under custom_components."""
    return


DEVICE_INFO: dict[str, Any] = {
    "status": "OK",
    "agentVersion": "1.0.0",
    "hostname": "KIOSK-PANEL-1",
    "deviceName": "Office Wall",
    "model": "Raspberry Pi 4 Model B Rev 1.4",
    "browserRunning": True,
    "browserRestartCount": 2,
    "browserLastExitCode": None,
    "browserVersion": "152.0.7977.82",
    "currentURL": "http://boards.invalid/alert-monitor/",
    "startURL": "http://boards.invalid/alert-monitor/",
    "screenOn": True,
    "screenBrightness": 200,
    "screenRawBrightness": 7,
    "screenBrightnessMax": 9,
    "screenInstrument": "wlr-randr",
    "displayOutput": "HDMI-A-1",
    "displayMake": "ASUSTek COMPUTER INC",
    "displayModel": "ROG XG27AQ",
    "resolution": "2560x1440",
    "orientation": "normal",
    "screensaverOn": False,
    "kioskMode": True,
    "maintenanceMode": False,
    "overlayMessage": "",
    "idleSeconds": 12.0,
    "uptimeSeconds": 3600.0,
    "cpuTemperatureC": 52.1,
    "availableMemoryMB": 1400.0,
    "totalMemoryMB": 3800.0,
    "memoryUsedPercent": 63.2,
    "freeStorageMB": 20000.0,
    "totalStorageMB": 29000.0,
    "storageUsedPercent": 31.0,
    "rootFilesystem": "rw",
    "throttle": {"ok": True, "flags": [], "raw": "0x0"},
    "wifi": {"ssid": "Example", "rssi": -43, "linkQuality": 63,
             "interface": "wlan0"},
    # RFC 7042 documentation-range MACs. NEVER a real one: a device address
    # committed to a public repository is estate inventory, and this repo has
    # already had one round of that scrubbed out of it.
    "interfaces": [
        {"name": "wlan0", "mac": "00:00:5e:00:53:01", "ipv4": HOST,
         "state": "up"},
        {"name": "eth0", "mac": "00:00:5e:00:53:02", "ipv4": None,
         "state": "down"},
    ],
}

SETTINGS: dict[str, Any] = {
    "status": "OK",
    "startURL": "http://boards.invalid/alert-monitor/",
    "errorURL": "http://ha.invalid/local/outage.html",
    "kioskMode": True,
    "maintenanceMode": False,
    "timeToScreenOffV2": 0,
    "timeToScreensaverV2": 900,
    "screenBrightness": 200,
    "screensaverBrightness": 20,
    "remoteAdminPassword": PASSWORD,
    "remoteAdmin": True,
    "remoteAdminPort": PORT,
}


def mock_agent(aioclient_mock: AiohttpClientMocker, *,
               device_info: dict[str, Any] | None = None,
               settings: dict[str, Any] | None = None) -> None:
    """Answer every command the integration issues.

    Matched on the query string, because that is how the agent distinguishes
    them — a mock keyed on the path alone would answer `listSettings` with a
    `deviceInfo` payload and every assertion downstream would still pass.
    """
    aioclient_mock.get(f"{BASE}?cmd=status", json={"status": "OK",
                                                   "agentVersion": "1.0.0"})
    aioclient_mock.get(f"{BASE}?cmd=deviceInfo",
                       json=device_info or DEVICE_INFO)
    aioclient_mock.get(f"{BASE}?cmd=listSettings", json=settings or SETTINGS)


@pytest.fixture()
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        title="Office Wall",
        unique_id="kiosk-panel-1",
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD, CONF_PORT: PORT},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture()
def no_app_probes():
    """The dashboard picker probes every configured app over HTTP.

    Patched out by default because the app list is empty in most tests and a
    live probe against a documentation-range address is a four-second stall
    per test for a list with nothing in it.
    """
    with patch(
        "custom_components.kiosk_pi.discovery.async_refresh_cache",
        return_value=[],
    ) as mocked:
        yield mocked
