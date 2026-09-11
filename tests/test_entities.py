"""Entity behaviour: what reads, what writes, and what refuses to guess."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.kiosk_pi.const import DOMAIN, UPDATE_INTERVAL

from .conftest import BASE, DEVICE_INFO, SETTINGS, mock_agent


async def setup(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory,
                  interval: timedelta = UPDATE_INTERVAL) -> None:
    """Move past one poll interval and let the refresh COMPLETE.

    DRAINED MORE THAN ONCE, DELIBERATELY, AND THE NUMBER IS MEASURED.
    `async_fire_time_changed` starts the refresh; the first drain only gets as
    far as the request being ISSUED, and the mocked response resolves on a
    later loop turn. A test that drains once holds the coordinator's previous
    data while the call log already shows the request — which reads exactly
    like a coordinator that fetched and ignored the answer, and was mistaken
    for one before this helper existed. Measured on this harness: the data is
    settled after the third drain, so the loop runs three and asserting after
    fewer is asserting on a half-finished refresh.
    """
    freezer.tick(interval + timedelta(seconds=1))
    async_fire_time_changed(hass)
    for _ in range(3):
        await hass.async_block_till_done()


def last_call(aioclient_mock: AiohttpClientMocker):
    return aioclient_mock.mock_calls[-1]


# --- readings ----------------------------------------------------------------

async def test_readings_come_off_the_panel(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    assert hass.states.get("sensor.office_wall_current_page").state == \
        "http://boards.invalid/alert-monitor/"
    assert hass.states.get("sensor.office_wall_cpu_temperature").state == "52.1"
    assert hass.states.get("sensor.office_wall_browser_restarts").state == "2"
    assert hass.states.get("sensor.office_wall_ip_address").state == "192.0.2.10"
    assert hass.states.get("binary_sensor.office_wall_browser").state == STATE_ON
    assert hass.states.get("switch.office_wall_screen").state == STATE_ON
    # Settings, from the second call of the same refresh.
    assert hass.states.get("number.office_wall_screensaver_timer").state == "900.0"
    assert hass.states.get("text.office_wall_start_url").state == \
        "http://boards.invalid/alert-monitor/"


async def test_the_ip_sensor_reads_the_interface_that_is_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """No interface name is assumed anywhere.

    The fleet is on wifi with eth0 down; a hardcoded wlan0 is the same class of
    mistake as a hardcoded hostname table.
    """
    info = {**DEVICE_INFO, "interfaces": [
        {"name": "wlan0", "mac": "aa:bb:cc:dd:ee:01", "ipv4": None, "state": "down"},
        {"name": "eth0", "mac": "aa:bb:cc:dd:ee:02", "ipv4": "192.0.2.99",
         "state": "up"},
    ]}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.office_wall_ip_address").state == "192.0.2.99"


async def test_an_unreadable_reading_is_unavailable_not_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """`ok at zero` and `could not read` do not collapse.

    A throttle sensor that reports "no problem" when the instrument was
    missing is exactly the reading a browning-out panel would give, and a
    screen switch that renders unknown power as `off` asserts something nobody
    measured.
    """
    info = {**DEVICE_INFO,
            "throttle": {"ok": None, "flags": [], "raw": None},
            "screenOn": None}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)

    assert hass.states.get("binary_sensor.office_wall_throttled").state == \
        STATE_UNAVAILABLE
    assert hass.states.get("switch.office_wall_screen").state == STATE_UNAVAILABLE


async def test_a_sticky_throttle_flag_is_a_problem(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    info = {**DEVICE_INFO,
            "throttle": {"ok": False, "flags": ["under_voltage_occurred"],
                         "raw": "0x10000"}}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_throttled").state == STATE_ON


async def test_brightness_is_unavailable_where_there_is_no_backlight(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A slider that moves and changes nothing teaches an operator it works.

    An HDMI monitor has no /sys/class/backlight device, so the agent reports no
    maximum and the control refuses to pretend.
    """
    info = {**DEVICE_INFO, "screenBrightnessMax": None,
            "screenRawBrightness": None}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("number.office_wall_screen_brightness").state == \
        STATE_UNAVAILABLE
    # The timers do NOT depend on a backlight and stay usable.
    assert hass.states.get("number.office_wall_screen_off_timer").state == "0.0"


# --- availability ------------------------------------------------------------

async def test_the_agent_sensor_stays_available_when_the_panel_goes_dark(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """A monitor that disappears with its subject cannot report the subject down.

    Every other entity going unavailable is correct — they describe a panel
    nobody can see. This one describes the READING, so it has to survive it.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_agent").state == STATE_ON

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}?cmd=deviceInfo", exc=TimeoutError())
    aioclient_mock.get(f"{BASE}?cmd=listSettings", exc=TimeoutError())
    await advance(hass, freezer)

    assert hass.states.get("binary_sensor.office_wall_agent").state == STATE_OFF
    assert hass.states.get("sensor.office_wall_cpu_temperature").state == \
        STATE_UNAVAILABLE


async def test_browser_backed_controls_are_unavailable_with_the_browser_down(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A control that is present and cannot work teaches that a red toast is normal.

    Restarting the browser is the exception and stays available: it is exactly
    what to press when the browser is down, and the agent owns the process
    rather than reaching it through DevTools.
    """
    mock_agent(aioclient_mock, device_info={**DEVICE_INFO,
                                            "browserRunning": False,
                                            "currentURL": None})
    await setup(hass, config_entry)

    assert hass.states.get("button.office_wall_load_start_url").state == \
        STATE_UNAVAILABLE
    assert hass.states.get("image.office_wall_screenshot").state == \
        STATE_UNAVAILABLE
    assert hass.states.get("button.office_wall_restart_browser").state != \
        STATE_UNAVAILABLE
    assert hass.states.get("button.office_wall_restart_device").state != \
        STATE_UNAVAILABLE


# --- the current-page dwell --------------------------------------------------

async def test_the_current_page_survives_a_restart_shaped_gap(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """FALL DWELL. A browser restart takes the tab away and brings it back.

    Publishing None in between makes every deliberate restart read as an
    outage.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.office_wall_current_page").state == \
        "http://boards.invalid/alert-monitor/"

    aioclient_mock.clear_requests()
    mock_agent(aioclient_mock, device_info={**DEVICE_INFO, "currentURL": None})
    await advance(hass, freezer)

    assert hass.states.get("sensor.office_wall_current_page").state == \
        "http://boards.invalid/alert-monitor/"


async def test_the_dwell_expires_rather_than_holding_a_stale_page_for_ever(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    aioclient_mock.clear_requests()
    mock_agent(aioclient_mock, device_info={**DEVICE_INFO, "currentURL": None})
    for _ in range(4):
        freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.office_wall_current_page").state == \
        STATE_UNKNOWN


async def test_a_new_page_is_published_at_once_with_no_rise_dwell(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes, freezer: FrozenDateTimeFactory,
) -> None:
    """Only the disappearance waits.

    Dwelling on the rise would delay the one reading an operator is watching
    for after pointing a wall somewhere new.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    aioclient_mock.clear_requests()
    mock_agent(aioclient_mock,
               device_info={**DEVICE_INFO, "currentURL": "http://b.invalid/new/"})
    await advance(hass, freezer)

    assert hass.states.get("sensor.office_wall_current_page").state == \
        "http://b.invalid/new/"


# --- writes ------------------------------------------------------------------

async def test_a_switch_writes_the_command_the_agent_expects(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    aioclient_mock.get(f"{BASE}?cmd=screenOff", json={"status": "OK"})
    await setup(hass, config_entry)

    await hass.services.async_call(
        "switch", "turn_off",
        {ATTR_ENTITY_ID: "switch.office_wall_screen"}, blocking=True,
    )
    assert any(str(url.query.get("cmd")) == "screenOff"
               for _m, url, _d, _h in aioclient_mock.mock_calls)


async def test_a_boolean_setting_is_written_through_the_boolean_setter(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """And `false` is written as `false`, not as a truthy string.

    Every value in a query string is a string, so a naive client makes a
    setting that can be turned on and never off — answering 200 both times.
    """
    mock_agent(aioclient_mock)
    aioclient_mock.get(f"{BASE}?cmd=setBooleanSetting",
                       json={"status": "OK", "key": "kioskMode", "value": False})
    await setup(hass, config_entry)

    await hass.services.async_call(
        "switch", "turn_off",
        {ATTR_ENTITY_ID: "switch.office_wall_kiosk_lock"}, blocking=True,
    )
    call = next(c for c in aioclient_mock.mock_calls
                if c[1].query.get("cmd") == "setBooleanSetting")
    assert call[1].query["key"] == "kioskMode"
    assert call[1].query["value"] == "false"


async def test_a_timer_accepts_zero_because_zero_means_never(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A minimum of 1 would make "never" unreachable from the UI."""
    mock_agent(aioclient_mock)
    aioclient_mock.get(f"{BASE}?cmd=setIntSetting", json={"status": "OK"})
    await setup(hass, config_entry)

    await hass.services.async_call(
        "number", "set_value",
        {ATTR_ENTITY_ID: "number.office_wall_screensaver_timer", "value": 0},
        blocking=True,
    )
    call = next(c for c in aioclient_mock.mock_calls
                if c[1].query.get("cmd") == "setIntSetting")
    assert call[1].query["value"] == "0"


async def test_the_load_url_service_is_transient_and_does_not_rewrite_the_start_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """An automation flashing a camera feed at a wall must not re-provision it.

    Persisting here would be discovered at the next reboot, by which point
    nobody connects the two.
    """
    mock_agent(aioclient_mock)
    aioclient_mock.get(f"{BASE}?cmd=loadURL", json={"status": "OK"})
    await setup(hass, config_entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, config_entry.entry_id)}
    )
    await hass.services.async_call(
        DOMAIN, "load_url",
        {"device_id": device.id, "url": "http://b.invalid/camera/"},
        blocking=True,
    )
    commands = [c[1].query.get("cmd") for c in aioclient_mock.mock_calls]
    assert "loadURL" in commands
    assert "setStringSetting" not in commands


async def test_a_service_call_against_a_foreign_device_is_an_error_not_a_no_op(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A call that quietly does nothing reports success.

    The caller then believes every target moved.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "load_url",
            {"device_id": "not-a-device", "url": "http://b.invalid/"},
            blocking=True,
        )
