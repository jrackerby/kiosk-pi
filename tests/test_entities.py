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
    assert hass.states.get("sensor.office_wall_ssid").state == "Example"
    assert hass.states.get("binary_sensor.office_wall_browser").state == STATE_ON
    assert hass.states.get("switch.office_wall_screen").state == STATE_ON
    # Settings, from the second call of the same refresh.
    assert hass.states.get("number.office_wall_screensaver_timer").state == "900.0"
    assert hass.states.get("text.office_wall_start_url").state == \
        "http://boards.invalid/alert-monitor/"


# --- crashes vs restarts, and the display (jrackerby/kiosk-pi#18) -------------

async def test_the_crash_count_is_its_own_reading_and_the_parts_sum(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Two commanded restarts are two restarts and ZERO crashes.

    Before 1.2.0 the fleet deploy's button press was indistinguishable from a
    dying browser; the whole point of the second entity is that this test
    can tell them apart.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    restarts = hass.states.get("sensor.office_wall_browser_restarts")
    crashes = hass.states.get("sensor.office_wall_browser_crashes")
    assert restarts.state == "2"
    assert crashes.state == "0"
    assert crashes.attributes["commanded_restarts"] == 2
    assert crashes.attributes["watchdog_restarts"] == 0
    assert crashes.attributes["last_exit_reason"] == "commanded"
    assert (int(crashes.state) + crashes.attributes["commanded_restarts"]
            + crashes.attributes["watchdog_restarts"]) == int(restarts.state)


async def test_a_crash_moves_the_crash_count(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    info = {**DEVICE_INFO, "browserRestartCount": 3, "browserCrashCount": 1,
            "browserLastExitReason": "crash", "browserLastExitCode": -9}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    crashes = hass.states.get("sensor.office_wall_browser_crashes")
    assert crashes.state == "1"
    assert crashes.attributes["last_exit_reason"] == "crash"
    assert crashes.attributes["last_exit_code"] == -9


async def test_an_old_agent_leaves_the_new_readings_unavailable_not_zero(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A 1.1.x agent sends none of the new keys. Absent is unknown, never a
    clean zero — a crash count of 0 read off an agent that cannot count
    crashes is the reading a crash-looping panel would give."""
    old = {k: v for k, v in DEVICE_INFO.items() if k not in (
        "browserCrashCount", "browserCommandedRestartCount",
        "browserWatchdogRestartCount", "browserLastExitReason",
        "displayConnected", "displayConnectors", "cpuFrequencyMHz",
        "coreVoltageV")}
    mock_agent(aioclient_mock, device_info=old)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.office_wall_browser_crashes").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.office_wall_display_connected").state == \
        STATE_UNAVAILABLE
    assert hass.states.get("sensor.office_wall_cpu_frequency").state == STATE_UNKNOWN
    assert hass.states.get("sensor.office_wall_core_voltage").state == STATE_UNKNOWN
    # And the old readings are untouched by the absence.
    assert hass.states.get("sensor.office_wall_browser_restarts").state == "2"


async def test_the_display_reading_is_the_kernels_not_the_compositors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    state = hass.states.get("binary_sensor.office_wall_display_connected")
    assert state.state == STATE_ON
    assert state.attributes["connectors"] == {"HDMI-A-1": "connected",
                                              "HDMI-A-2": "disconnected"}
    assert state.attributes["output"] == "HDMI-A-1"


async def test_a_blind_panel_reads_unplugged_while_the_browser_reads_on(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """jrackerby/HA#771's shape: cage up, Chromium up, nothing plugged in.

    Every browser-backed reading is unknown and `browser` is on; before this
    entity that was indistinguishable from a compositor that failed."""
    info = {**DEVICE_INFO, "displayConnected": False,
            "displayConnectors": [{"name": "HDMI-A-1", "status": "disconnected"}],
            "displayOutput": None, "currentURL": None, "resolution": None}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_display_connected").state == \
        STATE_OFF
    assert hass.states.get("binary_sensor.office_wall_browser").state == STATE_ON
    assert hass.states.get("sensor.office_wall_current_page").state == STATE_UNKNOWN


async def test_the_hardware_readings_carry_their_units(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    frequency = hass.states.get("sensor.office_wall_cpu_frequency")
    voltage = hass.states.get("sensor.office_wall_core_voltage")
    assert frequency.state == "1500.0"
    assert frequency.attributes["unit_of_measurement"] == "MHz"
    assert voltage.state == "0.936"
    assert voltage.attributes["unit_of_measurement"] == "V"


# --- the cursor (jrackerby/kiosk-pi#9) ---------------------------------------

async def test_the_cursor_reading_is_on_when_the_rule_applied(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """THE ONLY CURSOR READING THAT EXISTS OFF-DEVICE.

    `image.<panel>_screenshot` is a `Page.captureScreenshot`, taken out of
    Chromium's renderer compositor, while the stranded cursor is a wl_pointer
    surface handed to cage — so a screenshot shows no cursor whether or not one
    is on the glass. This entity is what a dashboard can actually read.
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_cursor_hidden").state \
        == STATE_ON


async def test_a_visible_cursor_reads_off_not_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """A panel whose extension never loaded. `auto` is a real answer, and it is
    the one that says the fix did not arrive on this wall."""
    info = {**DEVICE_INFO, "cursorStyle": "auto"}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_cursor_hidden").state \
        == STATE_OFF


async def test_the_cursor_reading_is_unavailable_when_the_feature_is_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """NOT `off`. With hideCursor disabled a pointer is the CORRECT state for a
    bench host somebody is driving, and publishing it as a problem teaches an
    operator to ignore this entity on the hosts where it matters."""
    info = {**DEVICE_INFO, "hideCursor": False, "cursorStyle": "auto"}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_cursor_hidden").state \
        == STATE_UNAVAILABLE


async def test_an_unreadable_cursor_is_unavailable_not_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """`could not read` is not `a cursor is showing`.

    Mapping a failed CDP read to False would publish a wall as faulty on the
    strength of not having looked — and on a panel whose browser is down, that
    is every poll.
    """
    info = {**DEVICE_INFO, "cursorStyle": None}
    mock_agent(aioclient_mock, device_info=info)
    await setup(hass, config_entry)
    assert hass.states.get("binary_sensor.office_wall_cursor_hidden").state \
        == STATE_UNAVAILABLE


async def test_the_raw_cursor_style_reaches_diagnostics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """The binary sensor answers "did the rule apply". The raw value answers
    "what did the page actually resolve to", which is what somebody debugging
    a wall needs, and it is not a credential."""
    from custom_components.kiosk_pi.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    mock_agent(aioclient_mock)
    await setup(hass, config_entry)
    dump = await async_get_config_entry_diagnostics(hass, config_entry)
    assert dump["device_info"]["cursorStyle"] == "none"


async def test_the_monitor_sensor_joins_make_and_model(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Both EDID halves reach the state, and both stay addressable separately.

    The agent used to publish ``model or make`` as one field, so a make was
    unrecoverable from the payload. Joining is the RENDER decision and belongs
    here; the attributes keep the two fields apart for anything templating over
    the fleet (jrackerby/kiosk-pi#7).
    """
    mock_agent(aioclient_mock)
    await setup(hass, config_entry)

    state = hass.states.get("sensor.office_wall_monitor")
    assert state.state == "ASUSTek COMPUTER INC ROG XG27AQ"
    assert state.attributes["make"] == "ASUSTek COMPUTER INC"
    assert state.attributes["model"] == "ROG XG27AQ"
    assert state.attributes["output"] == "HDMI-A-1"


async def test_a_monitor_reporting_only_one_edid_half_is_still_identified(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    """Half an EDID identifies a screen. A join that prints "None" does not.

    Either field may legitimately be absent, so the state carries whichever
    half arrived rather than a string with a hole in it — and an output with
    neither reads `unknown`, never an empty state.
    """
    make_only = {**DEVICE_INFO, "displayModel": None}
    mock_agent(aioclient_mock, device_info=make_only)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.office_wall_monitor").state == \
        "ASUSTek COMPUTER INC"


async def test_an_output_with_no_edid_at_all_reads_unknown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry,
    no_app_probes,
) -> None:
    neither = {**DEVICE_INFO, "displayMake": None, "displayModel": None}
    mock_agent(aioclient_mock, device_info=neither)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.office_wall_monitor").state == STATE_UNKNOWN


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
