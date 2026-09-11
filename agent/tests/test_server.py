"""The HTTP surface, exercised over a real loopback socket.

DELIBERATELY NOT A MOCKED HANDLER. The properties that matter here — that a bad
password is 401 and not 200-with-an-error-body, that an unknown command is 404,
that the password never reaches the log — are properties of the wire, and a test
that calls the dispatcher directly asserts none of them. The channel rule: a
check that exercises a different channel than the one that will be used
certifies nothing.

The agent under test never launches a browser. Every instrument it reaches for
is absent on a CI runner (no cage, no wlr-randr, no DevTools), which is exactly
the degraded state a real panel is in between a crash and a restart — so these
also assert that the API stays answerable when the wall is down.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from pikioskd.agent import KioskAgent
from pikioskd.server import (
    READ_ONLY_COMMANDS,
    CommandError,
    CommandTable,
    build_server,
)
from pikioskd.settings import Settings

PASSWORD = "test-password-not-a-real-secret"


@pytest.fixture()
def agent(tmp_path):
    settings = Settings(str(tmp_path / "settings.json"))
    settings.load()
    settings.set_many({
        "remoteAdminPassword": PASSWORD,
        "startURL": "http://example.invalid/board",
        "deviceName": "TESTPANEL",
    })
    return KioskAgent(settings)


@pytest.fixture()
def server(agent):
    # Port 0: the OS picks a free one. A fixed port makes the suite fail on a
    # runner that happens to have something on it, which reads as a broken
    # test rather than as a busy machine.
    instance = build_server(agent, host="127.0.0.1", port=0)
    instance.serve_in_thread()
    yield instance
    instance.shutdown()
    instance.server_close()


def call(server, **params):
    """Returns ``(status, body)``; body is parsed JSON where it is JSON."""
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as err:
        raw = err.read()
        status = err.code
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, raw


# --- authentication ----------------------------------------------------------

def test_no_password_is_401(server):
    status, body = call(server, cmd="status")
    assert status == 401
    assert body["status"] == "Error"


def test_wrong_password_is_401(server):
    status, _body = call(server, cmd="status", password="wrong")
    assert status == 401


def test_correct_password_is_200(server):
    status, body = call(server, cmd="status", password=PASSWORD)
    assert status == 200
    assert body["status"] == "OK"
    assert body["agentVersion"]


def test_an_empty_configured_password_refuses_everything(agent, server):
    """Fail CLOSED on an unconfigured device.

    The fail-open reading of "no password set" is an open remote-admin API on
    a LAN, reachable by anything that can guess the port.
    """
    agent.settings.set("remoteAdminPassword", "")
    assert call(server, cmd="status")[0] == 401
    assert call(server, cmd="status", password="")[0] == 401


def test_bearer_token_is_accepted(server):
    host, port = server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/?cmd=status",
        headers={"Authorization": f"Bearer {PASSWORD}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def test_remote_admin_off_is_403_not_401(agent, server):
    """Disabled and unauthorised are different conditions.

    Collapsing them sends an operator to re-check a password that was correct.
    """
    agent.settings.set("remoteAdmin", False)
    status, _body = call(server, cmd="status", password=PASSWORD)
    assert status == 403


def test_the_password_is_not_written_to_the_log(server, caplog):
    """BaseHTTPRequestHandler's default logger writes the full request line.

    The request line contains the password, so the default behaviour puts the
    shared secret in the journal on every single poll.
    """
    with caplog.at_level(logging.DEBUG):
        call(server, cmd="status", password=PASSWORD)
    assert PASSWORD not in caplog.text


# --- dispatch ----------------------------------------------------------------

def test_unknown_command_is_404_not_200(server):
    """Kiosker answers 200 to everything, an undefined function included.

    A fleet audit against that surface reads a guessed command name as live and
    passes over devices that understood nothing. The verdict belongs in the
    status code.
    """
    status, body = call(server, cmd="thisIsNotACommand", password=PASSWORD)
    assert status == 404
    assert body["status"] == "Error"


def test_no_command_is_400_and_lists_what_is_available(server):
    status, body = call(server, password=PASSWORD)
    assert status == 400
    assert "deviceInfo" in body["statustext"]


def test_missing_required_parameter_is_400(server):
    status, body = call(server, cmd="loadURL", password=PASSWORD)
    assert status == 400
    assert "url" in body["statustext"]


def test_a_dead_browser_is_502_not_500(server):
    """The agent is fine; the thing it depends on is not.

    That distinction is the difference between "restart the agent" and "the
    browser is down", and collapsing it sends every operator to the wrong
    place first. No DevTools endpoint exists in this test, which is precisely
    the state of a panel whose browser has crashed.
    """
    status, body = call(server, cmd="loadURL", password=PASSWORD,
                        url="http://example.invalid/")
    assert status == 502
    assert body["status"] == "Error"


# --- settings over the wire --------------------------------------------------

def test_list_settings_returns_typed_values(server):
    _status, body = call(server, cmd="listSettings", password=PASSWORD)
    assert isinstance(body["timeToScreenOffV2"], int)
    assert isinstance(body["kioskMode"], bool)


def test_set_string_setting_reads_back(server):
    status, body = call(server, cmd="setStringSetting", password=PASSWORD,
                        key="startURL", value="http://example.invalid/new")
    assert status == 200
    assert body["value"] == "http://example.invalid/new"
    assert body["changed"] is True


def test_setting_the_same_value_twice_reports_unchanged(server):
    call(server, cmd="setStringSetting", password=PASSWORD,
         key="deviceName", value="WALL")
    _status, body = call(server, cmd="setStringSetting", password=PASSWORD,
                         key="deviceName", value="WALL")
    assert body["changed"] is False


def test_boolean_setter_refuses_a_non_boolean_key(server):
    status, body = call(server, cmd="setBooleanSetting", password=PASSWORD,
                        key="startURL", value="true")
    assert status == 400
    assert "not a boolean setting" in body["statustext"]


def test_boolean_false_is_stored_as_false(server):
    """The coercion bug, over the wire.

    Every value in a query string is a string, so a naive implementation makes
    ``kioskMode=false`` store True — a setting that can be turned on and never
    off, answering 200 both times.
    """
    call(server, cmd="setBooleanSetting", password=PASSWORD,
         key="kioskMode", value="false")
    _status, body = call(server, cmd="listSettings", password=PASSWORD)
    assert body["kioskMode"] is False


def test_string_setter_coerces_an_int_key_rather_than_no_opping(server):
    """Fully answers 200 and silently no-ops here, so callers learned to
    write, re-read and fall back to the other setter. The obvious call works."""
    _status, body = call(server, cmd="setStringSetting", password=PASSWORD,
                         key="timeToScreenOffV2", value="120")
    assert body["value"] == 120


def test_out_of_range_value_is_400(server):
    status, _body = call(server, cmd="setIntSetting", password=PASSWORD,
                         key="screenBrightness", value="9999")
    assert status == 400


# --- the idle clock ----------------------------------------------------------

def test_polling_does_not_hold_the_screensaver_off(agent):
    """A read-only command is not an interaction.

    If it were, a Home Assistant coordinator polling every 30 seconds would
    hold the screensaver off forever — and it would look exactly like a broken
    timer setting rather than like a poll.
    """
    table = CommandTable(agent)
    with agent._lock:  # noqa: SLF001 - asserting on the clock this guards
        agent._last_interaction = 0.0
    for command in ("deviceInfo", "listSettings", "getCurrentURL", "status"):
        table.dispatch(command, {})
    with agent._lock:  # noqa: SLF001
        assert agent._last_interaction == 0.0


def test_a_mutating_command_is_an_interaction(agent):
    table = CommandTable(agent)
    with agent._lock:  # noqa: SLF001
        agent._last_interaction = 0.0
    table.dispatch("setStringSetting", {"key": "deviceName", "value": "X"})
    with agent._lock:  # noqa: SLF001
        assert agent._last_interaction > 0.0


def test_every_read_only_command_exists(agent):
    """A name in the set that is not a command can never fire.

    It would sit there looking like coverage while the real command's poll
    silently reset the idle clock.
    """
    assert READ_ONLY_COMMANDS <= set(CommandTable(agent).names())


# --- telemetry ---------------------------------------------------------------

def test_device_info_answers_with_the_wall_down(server):
    """No browser, no compositor, no DevTools — and it still answers.

    A monitor that disappears with its subject cannot report the subject down.
    """
    status, body = call(server, cmd="deviceInfo", password=PASSWORD)
    assert status == 200
    assert body["browserRunning"] is False
    assert body["hostname"]
    assert body["deviceName"] == "TESTPANEL"
    assert body["startURL"] == "http://example.invalid/board"
    # Absent instruments read as unknown, never as a value.
    assert body["currentURL"] is None
    assert body["screenOn"] is None


def test_the_assertions_above_can_fail(server):
    """An assertion set needs a self-test proving it CAN fail."""
    with pytest.raises(AssertionError):
        assert call(server, cmd="status")[0] == 200          # unauthenticated
    with pytest.raises(AssertionError):
        assert call(server, cmd="nope", password=PASSWORD)[0] == 200


# --- rebootDevice ------------------------------------------------------------
#
# THE SCHEDULED SHELL IS DETACHED AND ITS FAILURE REACHES NOBODY, so the only
# thing standing between "sudo is refused" and a panel that answers OK and
# never reboots is the pre-flight probe. Both subprocess entry points are
# patched in every test below: a suite that actually reached `systemctl reboot`
# would take the CI runner down with it.

def test_a_refused_sudo_is_a_502_not_a_cheerful_ok(agent, monkeypatch):
    """The exact production failure: NoNewPrivileges=yes in the unit.

    sudo refuses to elevate at all under PR_SET_NO_NEW_PRIVS, and its own
    stderr says so — which is why the probe's stderr is passed through rather
    than replaced with a generic message.
    """
    import subprocess

    from pikioskd import server as server_module

    class Refused:
        returncode = 1
        stdout = ""
        stderr = ('sudo: The "no new privileges" flag is set, which prevents '
                  "sudo from running as root.")

    launched: list[Any] = []
    monkeypatch.setattr(server_module.subprocess, "run",
                        lambda *a, **k: Refused())
    monkeypatch.setattr(server_module.subprocess, "Popen",
                        lambda *a, **k: launched.append(a))

    table = CommandTable(agent)
    with pytest.raises(CommandError) as caught:
        table.dispatch("rebootDevice", {})

    assert caught.value.status == 502
    assert "no new privileges" in str(caught.value)
    assert not launched, "the reboot was scheduled despite sudo being refused"


def test_a_working_sudo_schedules_the_reboot(agent, monkeypatch):
    from pikioskd import server as server_module

    class Allowed:
        returncode = 0
        stdout = ""
        stderr = ""

    launched: list[Any] = []
    monkeypatch.setattr(server_module.subprocess, "run",
                        lambda *a, **k: Allowed())
    monkeypatch.setattr(server_module.subprocess, "Popen",
                        lambda *a, **k: launched.append(a[0]))

    result = CommandTable(agent).dispatch("rebootDevice", {})
    assert result["rebooting"] is True
    assert len(launched) == 1
    # Delayed, so the 200 leaves before the host goes down.
    assert "sleep 1" in launched[0][-1]
    assert "systemctl reboot" in launched[0][-1]


def test_the_probe_is_a_no_op_and_not_the_reboot_itself(agent, monkeypatch):
    """A probe that ran `systemctl reboot` to see if it could would reboot.

    Asserted because the obvious shortcut — "just try the real command" — is
    catastrophic here rather than merely wrong.
    """
    from pikioskd import server as server_module

    seen: list[list[str]] = []

    class Allowed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, *a, **k):
        seen.append(list(argv))
        return Allowed()

    monkeypatch.setattr(server_module.subprocess, "run", fake_run)
    monkeypatch.setattr(server_module.subprocess, "Popen", lambda *a, **k: None)
    CommandTable(agent).dispatch("rebootDevice", {})

    assert seen == [["sudo", "-n", "true"]]


def test_a_settings_write_that_cannot_persist_says_so(server, agent, monkeypatch):
    """500 with a reason, not a cheerful 200 and not a bare "internal error".

    The operator needs two facts the generic handler gives neither of: that the
    filesystem refused, and that the value was rolled back — so what the agent
    reports now is still what is on disk.
    """
    def refuse() -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(agent.settings, "save", refuse)
    status, body = call(server, cmd="setStringSetting", password=PASSWORD,
                        key="deviceName", value="WALL")

    assert status == 500
    assert "NOT saved" in body["statustext"]
    assert "Read-only file system" in body["statustext"]

    # And the reported value is the one still on disk.
    _status, settings = call(server, cmd="listSettings", password=PASSWORD)
    assert settings["deviceName"] == "TESTPANEL"
