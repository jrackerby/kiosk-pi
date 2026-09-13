"""Auto-recovery: exit classification, the liveness watchdog, sd_notify, DRM.

THE SUPERVISOR IS EXERCISED AGAINST A REAL CHILD PROCESS. The exit classes
are properties of what happens between ``_terminate`` and ``process.wait()``
in the supervision thread, and a test that calls ``_record_exit`` directly
would assert the bookkeeping while missing the race it exists to resolve.
The "browser" is ``python3 -c sleep`` in its own process group, which is all
the supervisor ever sees of Chromium anyway: a pid, a group, an exit code.
"""

from __future__ import annotations

import os
import pathlib
import signal
import socket
import sys
import threading
import time

import pytest

from pikioskd import device, sdnotify
from pikioskd.agent import TICK_SECONDS, KioskAgent
from pikioskd.browser import BrowserSupervisor
from pikioskd.settings import Settings

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def settings(tmp_path):
    store = Settings(str(tmp_path / "settings.json"))
    store.load()
    store.set("browserRestartBackoffSeconds", 0)
    return store


@pytest.fixture()
def supervisor(settings, monkeypatch):
    sup = BrowserSupervisor(settings)
    monkeypatch.setattr(BrowserSupervisor, "command", lambda self, url: SLEEPER)
    sup.start()
    assert _wait_for(sup.is_running), "the sleeper never launched"
    yield sup
    sup.shutdown()


# --- exit classification ------------------------------------------------------

def test_a_commanded_restart_is_not_a_crash(supervisor):
    supervisor.restart()
    assert _wait_for(lambda: supervisor.restart_count == 1)
    assert supervisor.last_exit_reason == "commanded"
    assert supervisor.commanded_restart_count == 1
    assert supervisor.crash_count == 0
    # And it came back.
    assert _wait_for(supervisor.is_running)


def test_an_exit_nobody_asked_for_is_a_crash(supervisor):
    pid = supervisor._process.pid
    os.killpg(pid, signal.SIGKILL)
    assert _wait_for(lambda: supervisor.restart_count == 1)
    assert supervisor.last_exit_reason == "crash"
    assert supervisor.crash_count == 1
    assert supervisor.commanded_restart_count == 0
    assert supervisor.last_exit_code is not None


def test_a_watchdog_recovery_is_counted_on_its_own(supervisor):
    supervisor.recover("test")
    assert _wait_for(lambda: supervisor.restart_count == 1)
    assert supervisor.last_exit_reason == "watchdog"
    assert supervisor.watchdog_restart_count == 1
    assert supervisor.crash_count == 0


def test_the_three_parts_sum_to_the_total(supervisor):
    supervisor.restart()
    assert _wait_for(lambda: supervisor.restart_count == 1)
    assert _wait_for(supervisor.is_running)
    os.killpg(supervisor._process.pid, signal.SIGKILL)
    assert _wait_for(lambda: supervisor.restart_count == 2)
    assert (supervisor.crash_count + supervisor.commanded_restart_count
            + supervisor.watchdog_restart_count) == supervisor.restart_count


def test_a_commanded_exit_does_not_compound_the_backoff(settings, monkeypatch):
    """Two deploys ten seconds apart must not leave the wall waiting out a
    penalty. The crash path doubles; the commanded path uses the configured
    delay every time."""
    settings.set("browserRestartBackoffSeconds", 0)
    sup = BrowserSupervisor(settings)
    monkeypatch.setattr(BrowserSupervisor, "command", lambda self, url: SLEEPER)
    waits: list[float] = []
    monkeypatch.setattr(sup, "_wait", lambda seconds: waits.append(seconds))
    sup.start()
    try:
        assert _wait_for(sup.is_running)
        for expected in (1, 2, 3):
            sup.restart()
            assert _wait_for(lambda: sup.restart_count == expected)
            assert _wait_for(sup.is_running)
    finally:
        sup.shutdown()
    assert waits and all(w == 0 for w in waits), waits


def test_the_classification_can_fail():
    """Self-test: a supervisor that never exited has no reason, and the
    counters start at zero, so an assertion on them CAN be wrong."""
    sup = BrowserSupervisor(Settings.__new__(Settings))
    with pytest.raises(AssertionError):
        assert sup.last_exit_reason == "crash"
    with pytest.raises(AssertionError):
        assert sup.crash_count == 1


# --- the liveness watchdog ----------------------------------------------------

@pytest.fixture()
def silent(settings, monkeypatch):
    """A supervisor whose browser is 'running' and never answers DevTools."""
    sup = BrowserSupervisor(settings)
    monkeypatch.setattr(sup, "is_running", lambda: True)
    monkeypatch.setattr(sup, "responsive", lambda: False)
    acted: list[str] = []
    monkeypatch.setattr(sup, "recover", lambda why: acted.append(why))
    sup._last_start = 1000.0
    return sup, acted


def test_a_silent_browser_on_a_connected_output_is_restarted(silent):
    sup, acted = silent
    sup.note_display(True, now=1000.0)
    assert sup.check_liveness(now=1000.0 + 119) is None
    assert acted == []
    assert sup.check_liveness(now=1000.0 + 120) == "watchdog"
    assert len(acted) == 1


def test_nothing_plugged_in_means_no_restart_ever(silent):
    """The bench host (jrackerby/HA#771): cage up, Chromium up, no CDP by
    design. Restarting it would loop until somebody plugged a monitor in."""
    sup, acted = silent
    sup.note_display(False, now=1000.0)
    assert sup.check_liveness(now=1000.0 + 10_000) is None
    assert acted == []


def test_an_unreadable_connector_state_is_not_a_licence_to_act(silent):
    sup, acted = silent
    sup.note_display(None, now=1000.0)
    assert sup.check_liveness(now=1000.0 + 10_000) is None
    assert acted == []


def test_zero_disables_the_watchdog(silent, settings):
    sup, acted = silent
    settings.set("browserHangSeconds", 0)
    sup.note_display(True, now=1000.0)
    assert sup.check_liveness(now=1000.0 + 10_000) is None
    assert acted == []


def test_a_reconnect_restarts_the_clock(silent):
    sup, acted = silent
    sup.note_display(False, now=1000.0)
    sup.note_display(True, now=2000.0)
    # 119s after the reconnect, however long since launch: not yet.
    assert sup.check_liveness(now=2000.0 + 119) is None
    assert sup.check_liveness(now=2000.0 + 120) == "watchdog"


def test_a_browser_that_answered_recently_is_left_alone(settings, monkeypatch):
    sup = BrowserSupervisor(settings)
    monkeypatch.setattr(sup, "is_running", lambda: True)
    answers = iter([True, False, False])
    monkeypatch.setattr(sup, "responsive", lambda: next(answers))
    acted: list[str] = []
    monkeypatch.setattr(sup, "recover", lambda why: acted.append(why))
    sup._last_start = 0.0
    sup.note_display(True, now=0.0)
    assert sup.check_liveness(now=5000.0) is None       # answered: clock reset
    assert sup.check_liveness(now=5000.0 + 60) is None  # silent, under limit
    assert sup.check_liveness(now=5000.0 + 120) == "watchdog"
    assert len(acted) == 1


def test_a_reconnect_cuts_a_pending_backoff_short(settings):
    """Five minutes of black glass after the monitor came back is the
    failure this exists to prevent."""
    sup = BrowserSupervisor(settings)
    sup.note_display(False)
    finished = threading.Event()

    def wait():
        sup._wait(30.0)
        finished.set()

    threading.Thread(target=wait, daemon=True).start()
    time.sleep(0.2)
    assert not finished.is_set()
    sup.note_display(True)
    assert finished.wait(2.0), "the backoff did not wake on reconnect"


def test_a_disconnect_does_not_wake_the_backoff(settings):
    sup = BrowserSupervisor(settings)
    sup.note_display(True)
    finished = threading.Event()

    def wait():
        sup._wait(1.0)
        finished.set()

    started = time.monotonic()
    threading.Thread(target=wait, daemon=True).start()
    time.sleep(0.1)
    sup.note_display(False)
    finished.wait(3.0)
    assert time.monotonic() - started >= 0.9


# --- the agent's own health, as fed to systemd ---------------------------------

def test_the_loop_is_unhealthy_before_it_starts(settings):
    agent = KioskAgent(settings)
    assert agent.loop_healthy() is False


def test_a_wedged_tick_stops_the_pings(settings, monkeypatch):
    agent = KioskAgent(settings)
    monkeypatch.setattr(agent.browser, "start", lambda: None)
    agent.start()
    try:
        assert _wait_for(lambda: agent.last_tick_at is not None, timeout=TICK_SECONDS + 5)
        assert agent.loop_healthy()
        # The same live thread, judged from a clock six ticks later.
        assert agent.loop_healthy(now=time.time() + TICK_SECONDS * 6 + 1) is False
    finally:
        agent.shutdown()


# --- sd_notify -----------------------------------------------------------------

def test_no_socket_is_a_no_op_that_says_so():
    assert sdnotify.notify("READY=1", environ={}) is False


def test_the_datagram_reaches_a_listening_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(path)
        listener.settimeout(2.0)
        assert sdnotify.notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": path}) is True
        assert listener.recv(64) == b"WATCHDOG=1"


def test_an_abstract_socket_address_is_understood():
    name = f"@pikioskd-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind("\0" + name[1:])
        listener.settimeout(2.0)
        assert sdnotify.notify("READY=1", environ={"NOTIFY_SOCKET": name}) is True
        assert listener.recv(64) == b"READY=1"


def test_a_dead_socket_is_false_not_an_exception(tmp_path):
    assert sdnotify.notify("READY=1", environ={
        "NOTIFY_SOCKET": str(tmp_path / "nobody-listens")}) is False


def test_the_watchdog_interval_is_only_ours_when_the_pid_matches():
    mine = str(os.getpid())
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "90000000",
                                       "WATCHDOG_PID": mine}) == 90.0
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "90000000",
                                       "WATCHDOG_PID": "1"}) is None
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "90000000"}) == 90.0
    assert sdnotify.watchdog_interval({}) is None
    assert sdnotify.watchdog_interval({"WATCHDOG_USEC": "junk"}) is None


# --- DRM connectors --------------------------------------------------------------

def test_connector_names_join_on_the_compositors_spelling():
    assert device.parse_connector_name("/sys/class/drm/card0-HDMI-A-1/status") == "HDMI-A-1"
    assert device.parse_connector_name("/sys/class/drm/card1-DSI-1/status") == "DSI-1"
    assert device.parse_connector_name("/sys/class/drm/card0/status") is None


def _sysfs(tmp_path: pathlib.Path, **status: str) -> str:
    for name, value in status.items():
        directory = tmp_path / f"card0-{name.replace('_', '-')}"
        directory.mkdir()
        (directory / "status").write_text(value + "\n")
    return str(tmp_path / "card*-*" / "status")


def test_connectors_are_read_and_writeback_is_not_a_screen(tmp_path):
    pattern = _sysfs(tmp_path, HDMI_A_1="connected", HDMI_A_2="disconnected",
                     Writeback_1="connected")
    records = device.drm_connectors(pattern)
    assert records == [
        {"name": "HDMI-A-1", "status": "connected"},
        {"name": "HDMI-A-2", "status": "disconnected"},
    ]
    assert device.display_connected(records) is True


def test_nothing_plugged_in_reads_false(tmp_path):
    pattern = _sysfs(tmp_path, HDMI_A_1="disconnected")
    assert device.display_connected(device.drm_connectors(pattern)) is False


def test_no_drm_at_all_reads_unknown(tmp_path):
    assert device.drm_connectors(str(tmp_path / "nothing*" / "status")) is None
    assert device.display_connected(None) is None


def test_unknown_is_not_disconnected(tmp_path):
    """A connector the kernel cannot read is left out of the decision, and
    with nothing decidable the answer is unknown — never False, which would
    license a restart on every host that lacks hotplug detect."""
    pattern = _sysfs(tmp_path, HDMI_A_1="unknown")
    assert device.display_connected(device.drm_connectors(pattern)) is None
    with pytest.raises(AssertionError):
        assert device.display_connected(device.drm_connectors(pattern)) is False


# --- the two firmware readings -----------------------------------------------------

def test_core_voltage_is_parsed_from_the_firmware_line():
    assert device.parse_core_voltage("volt=0.9360V\n") == 0.936
    assert device.parse_core_voltage("volt=1.3500V") == 1.35
    assert device.parse_core_voltage("") is None
    assert device.parse_core_voltage("error") is None


def test_the_new_readings_are_in_the_payload():
    info = device.base_info()
    for key in ("cpuFrequencyMHz", "coreVoltageV"):
        assert key in info
    # Whatever the host, the type contract holds: a number or None.
    assert info["cpuFrequencyMHz"] is None or isinstance(info["cpuFrequencyMHz"], float)
    assert info["coreVoltageV"] is None or isinstance(info["coreVoltageV"], float)
