"""Flag merging, the outage-page contract, output parsing, and host readings.

All four are pure functions over text or over a settings object, so none of
these tests needs a browser, a compositor or a Pi.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from pikioskd import device
from pikioskd.browser import BASE_FLAGS, BrowserSupervisor, merge_flags
from pikioskd.cdp import CDPError
from pikioskd.display import Display
from pikioskd.settings import Settings

# A real `wlr-randr` transcript shape: header line per output, indented
# properties, one mode line marked current.
WLR_RANDR = """\
HDMI-A-1 "ASUSTek COMPUTER INC ROG XG27AQ M1LMQS000811 (HDMI-A-1)"
  Make: ASUSTek COMPUTER INC
  Model: ROG XG27AQ
  Serial: M1LMQS000811
  Physical size: 600x340 mm
  Enabled: yes
  Modes:
    2560x1440 px, 59.951000 Hz (preferred, current)
    1920x1080 px, 60.000000 Hz
  Position: 0,0
  Transform: normal
  Scale: 1.000000
HDMI-A-2 "Unknown"
  Enabled: no
"""


@pytest.fixture()
def settings(tmp_path):
    store = Settings(str(tmp_path / "settings.json"))
    store.load()
    return store


# --- flags -------------------------------------------------------------------

def test_operator_flag_replaces_the_base_flag_of_the_same_name():
    """Chromium keeps ONE of a repeated flag and does not say which.

    Leaving both in and hoping the last wins is how a fleet's disabled-features
    set silently reverts to the base value on some hosts and not others.
    """
    merged = merge_flags(("--kiosk", "--disable-features=TranslateUI"),
                         ["--disable-features=A,B"])
    assert merged == ["--kiosk", "--disable-features=A,B"]
    assert "--disable-features=TranslateUI" not in merged


def test_unrelated_operator_flags_are_appended():
    merged = merge_flags(("--kiosk",), ["--force-device-scale-factor=1.25"])
    assert merged == ["--kiosk", "--force-device-scale-factor=1.25"]


def test_launch_command_carries_the_devtools_port(settings):
    settings.set("cdpPort", 9333)
    argv = BrowserSupervisor(settings).command("http://example.invalid/")
    assert "--remote-debugging-port=9333" in argv
    assert argv[-1] == "http://example.invalid/"
    assert argv[0] == settings.get("cageBinary")


def test_kiosk_mode_off_drops_the_kiosk_flag(settings):
    settings.set("kioskMode", False)
    argv = BrowserSupervisor(settings).command("http://example.invalid/")
    assert "--kiosk" not in argv


def test_kiosk_mode_on_keeps_it(settings):
    argv = BrowserSupervisor(settings).command("http://example.invalid/")
    assert "--kiosk" in argv


# --- the cursor (jrackerby/kiosk-pi#9, #4) ------------------------------------

def test_there_is_exactly_one_disable_features_flag():
    """Chromium keeps ONE of a repeated flag and does not say which.

    `--load-extension` needs a feature disabled to work at all from Chromium
    137, and the base set already disabled one for the translate bar. Appending
    a second `--disable-features` would have silently dropped whichever one
    Chromium felt like — so the two are COMPOSED into one value, and this
    asserts on the count rather than on the presence of either.
    """
    from pikioskd.browser import DISABLED_FEATURES

    flags = [f for f in BASE_FLAGS if f.startswith("--disable-features=")]
    assert len(flags) == 1

    values = flags[0].split("=", 1)[1].split(",")
    assert set(values) == set(DISABLED_FEATURES)
    assert len(values) == len(set(values)), "a feature listed twice"


def test_no_feature_is_disabled_on_a_guess():
    """The branded-Chrome escape hatch is NOT carried here, deliberately.

    Google-branded Chrome restricted `--load-extension` at 137 and removed it,
    along with its `DisableLoadExtensionCommandLineSwitch` workaround, at 142.
    Unbranded Chromium — what Raspberry Pi OS packages and what
    `chromiumBinary` defaults to — keeps the switch, and the live fleet reports
    152.0.7977.82 from that package. Carrying the workaround anyway would have
    written a version gate that does not govern this fleet into the flag set,
    which is the same class of defect as the cursor comment this replaced.
    """
    from pikioskd.browser import DISABLED_FEATURES

    assert "DisableLoadExtensionCommandLineSwitch" not in DISABLED_FEATURES


def test_the_cursor_extension_is_written_and_loaded(settings, tmp_path):
    settings.set("chromiumProfileDir", str(tmp_path / "profile" / "chromium"))
    supervisor = BrowserSupervisor(settings)

    argv = supervisor.command("http://example.invalid/")

    directory = supervisor.cursor_extension_dir()
    assert f"--load-extension={directory}" in argv
    assert os.path.isdir(directory)

    manifest = json.loads(
        (pathlib.Path(directory) / "manifest.json").read_text()
    )
    assert manifest["manifest_version"] == 3
    assert manifest["content_scripts"][0]["css"] == ["hide-cursor.css"]
    # document_start, or the arrow is painted before the rule lands.
    assert manifest["content_scripts"][0]["run_at"] == "document_start"

    css = (pathlib.Path(directory) / "hide-cursor.css").read_text()
    assert "cursor: none !important" in css


def test_the_extension_lives_outside_the_chromium_profile(settings, tmp_path):
    """Chromium rewrites its profile directory as it pleases. An extension it
    is being asked to load from inside one is a directory two writers own."""
    profile = tmp_path / "profile" / "chromium"
    settings.set("chromiumProfileDir", str(profile))
    directory = pathlib.Path(BrowserSupervisor(settings).cursor_extension_dir())

    assert profile not in directory.parents
    assert directory != profile


def test_hide_cursor_off_loads_nothing(settings, tmp_path):
    """A bench host somebody is actually driving needs its pointer."""
    settings.set("chromiumProfileDir", str(tmp_path / "profile" / "chromium"))
    settings.set("hideCursor", False)

    argv = BrowserSupervisor(settings).command("http://example.invalid/")
    assert not [f for f in argv if f.startswith("--load-extension=")]


def test_an_unwritable_state_dir_costs_the_cursor_not_the_wall(settings,
                                                               tmp_path):
    """A visible cursor is a blemish. A wall that will not start is an outage.

    Asserted through `command()` rather than by calling the writer directly:
    the question is whether the LAUNCH survives, and a writer that raises is
    only a defect because of what it would do to the argv.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    settings.set("chromiumProfileDir", str(blocker / "chromium"))

    argv = BrowserSupervisor(settings).command("http://example.invalid/")

    assert not [f for f in argv if f.startswith("--load-extension=")]
    assert "--kiosk" in argv
    assert argv[-1] == "http://example.invalid/"


def test_an_operator_flag_can_still_override_disable_features(settings,
                                                              tmp_path):
    """The operator wins, and loses the extension with it — which is correct
    and is the documented consequence of replacing the whole value."""
    settings.set("chromiumProfileDir", str(tmp_path / "profile" / "chromium"))
    settings.set("chromiumFlags", ["--disable-features=SomethingElse"])

    argv = BrowserSupervisor(settings).command("http://example.invalid/")
    features = [f for f in argv if f.startswith("--disable-features=")]
    assert features == ["--disable-features=SomethingElse"]


def test_cursor_style_reads_the_live_document(settings, monkeypatch):
    """Read off documentElement, not body.

    An interstitial and a board that has not painted yet both have a
    documentElement; `body` is null on the former, and a reader that asks for
    it gets a TypeError out of the page rather than a cursor.
    """
    supervisor = BrowserSupervisor(settings)
    monkeypatch.setattr(supervisor, "is_running", lambda: True)
    seen = {}

    class FakeCDP:
        def evaluate(self, expression):
            seen["expr"] = expression
            return "none"

    monkeypatch.setattr(supervisor, "cdp", lambda: FakeCDP())

    assert supervisor.cursor_style() == "none"
    assert "documentElement" in seen["expr"]
    assert "body" not in seen["expr"]


def test_cursor_style_is_none_when_the_browser_is_down(settings, monkeypatch):
    """Not a timeout on every poll of a panel already known to be dark."""
    supervisor = BrowserSupervisor(settings)
    monkeypatch.setattr(supervisor, "is_running", lambda: False)

    def explode():
        raise AssertionError("cdp() must not be reached with the browser down")

    monkeypatch.setattr(supervisor, "cdp", explode)
    assert supervisor.cursor_style() is None


def test_a_cdp_failure_is_unknown_not_a_visible_cursor(settings, monkeypatch):
    """`could not read` and `a cursor is showing` are different values.

    Coercing a failed read to a truthy style would publish a panel as having a
    visible cursor on the strength of not having looked.
    """
    supervisor = BrowserSupervisor(settings)
    monkeypatch.setattr(supervisor, "is_running", lambda: True)

    class DeadCDP:
        def evaluate(self, expression):
            raise CDPError("browser hung up")

    monkeypatch.setattr(supervisor, "cdp", lambda: DeadCDP())
    assert supervisor.cursor_style() is None


@pytest.mark.parametrize("answer", [None, 42, "", {"cursor": "none"}])
def test_an_unusable_answer_is_unknown_never_stringified(settings, monkeypatch,
                                                         answer):
    """`None` stringified is the word "None", which reads as a real cursor
    value nobody can look up."""
    supervisor = BrowserSupervisor(settings)
    monkeypatch.setattr(supervisor, "is_running", lambda: True)

    class OddCDP:
        def evaluate(self, expression):
            return answer

    monkeypatch.setattr(supervisor, "cdp", lambda: OddCDP())
    assert supervisor.cursor_style() is None


def test_base_flags_have_no_duplicate_names():
    names = [flag.split("=", 1)[0] for flag in BASE_FLAGS]
    assert len(names) == len(set(names))


# --- the outage page ---------------------------------------------------------

def test_error_url_carries_the_estate_contract(settings):
    """``panel``, ``back``, ``error``, ``url`` — the shape outage.html reads.

    Asserted because it is a cross-repository contract with no compiler
    between the two ends: the page is served from Home Assistant and read by
    a browser this agent points at, so nothing but this test connects them.
    """
    supervisor = BrowserSupervisor(settings)
    url = supervisor.error_url_for(
        "http://ha.invalid:8123/local/outage.html",
        "http://boards.invalid/alert-monitor/",
        "net::ERR_ADDRESS_UNREACHABLE",
        "KIOSK-PANEL-1",
    )
    assert url.startswith("http://ha.invalid:8123/local/outage.html?")
    assert "panel=KIOSK-PANEL-1" in url
    assert "error=net%3A%3AERR_ADDRESS_UNREACHABLE" in url
    assert "back=http%3A%2F%2Fboards.invalid%2Falert-monitor%2F" in url


def test_error_url_preserves_the_templates_own_query(settings):
    url = BrowserSupervisor(settings).error_url_for(
        "http://ha.invalid/outage.html?theme=dark", "http://x.invalid/", "", "P"
    )
    assert "theme=dark" in url
    assert "panel=P" in url


def test_error_url_omits_error_when_the_code_is_unknown(settings):
    url = BrowserSupervisor(settings).error_url_for(
        "http://ha.invalid/outage.html", "http://x.invalid/", "", "P"
    )
    assert "error=" not in url


# --- display -----------------------------------------------------------------

def test_wlr_randr_output_is_parsed():
    outputs = Display().parse_outputs(WLR_RANDR)
    assert [o["name"] for o in outputs] == ["HDMI-A-1", "HDMI-A-2"]
    first = outputs[0]
    assert first["model"] == "ROG XG27AQ"
    assert first["enabled"] is True
    assert first["mode"] == "2560x1440"
    assert first["transform"] == "normal"
    assert outputs[1]["enabled"] is False


def test_a_disabled_output_is_not_reported_as_enabled():
    """The fail-permissive shape: a parse miss defaulting to on.

    A wall whose output is off but reads on is exactly the reading that makes a
    dark panel look healthy on a dashboard.
    """
    outputs = Display().parse_outputs("HDMI-A-1 \"x\"\n  Enabled: no\n")
    assert outputs[0]["enabled"] is False


def test_unknown_properties_do_not_break_known_ones():
    text = WLR_RANDR.replace("  Position: 0,0\n",
                             "  Position: 0,0\n  SomeFutureKey: 42\n")
    assert Display().parse_outputs(text)[0]["model"] == "ROG XG27AQ"


def test_state_reports_make_and_model_as_two_fields(monkeypatch):
    """Collapsing them loses the make outright (jrackerby/kiosk-pi#7).

    ``state()`` used to publish ``model or make`` under one key, so a screen
    reporting only a make was indistinguishable from one reporting only a
    model, and the make could not be recovered by any consumer. Asserting on
    BOTH keys is what makes the collapse fail here: an assertion on `model`
    alone passes against the old code.
    """
    display = Display()
    monkeypatch.setattr(display, "outputs",
                        lambda: display.parse_outputs(WLR_RANDR))
    monkeypatch.setattr(display, "brightness", lambda: (None, None))

    record = display.state()
    assert record["make"] == "ASUSTek COMPUTER INC"
    assert record["model"] == "ROG XG27AQ"


def test_an_output_with_no_edid_reports_neither_half(monkeypatch):
    """Absent is absent. A make standing in for a missing model is a lie.

    HDMI-A-2 in the fixture carries no Make: and no Model: line at all, so both
    keys must come back None rather than one borrowing the other's value.
    """
    display = Display(output_name="HDMI-A-2")
    monkeypatch.setattr(display, "outputs",
                        lambda: display.parse_outputs(WLR_RANDR))
    monkeypatch.setattr(display, "brightness", lambda: (None, None))

    record = display.state()
    assert record["output"] == "HDMI-A-2"
    assert record["make"] is None
    assert record["model"] is None


# --- host readings -----------------------------------------------------------

def test_throttle_flags_are_named():
    """The high half is sticky: a 3am brownout still reads at noon.

    0x50005 sets bits 0 and 2 (under-voltage and throttled RIGHT NOW) and bits
    16 and 18 (the same two, sticky since boot). Bit 17 is deliberately NOT set
    here, so a decoder that named every bit in the high half regardless would
    fail this test rather than pass it.
    """
    decoded = device.decode_throttle("throttled=0x50005")
    assert decoded["ok"] is False
    assert decoded["flags"] == [
        "currently_throttled",
        "throttling_occurred",
        "under_voltage",
        "under_voltage_occurred",
    ]


def test_zero_throttle_is_ok():
    assert device.decode_throttle("throttled=0x0")["ok"] is True


def test_unreadable_throttle_is_unknown_not_clean():
    """``ok at zero`` and ``could not read`` are different values.

    A throttle report defaulting to healthy is precisely the reading a
    browning-out panel would give.
    """
    assert device.decode_throttle(None)["ok"] is None
    assert device.decode_throttle("")["ok"] is None
    assert device.decode_throttle("nonsense")["ok"] is None


def test_wireless_is_parsed_from_the_kernel_file():
    raw = (
        "Inter-| sta-|   Quality        |   Discarded packets               |\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc\n"
        " wlan0: 0000   63.  -47.  -256        0      0      0      0      0\n"
    )
    record = device.parse_wireless(raw)
    assert record == {"interface": "wlan0", "linkQuality": 63, "rssi": -47}


def test_wireless_absent_reads_unknown_not_zero():
    assert device.parse_wireless(None)["rssi"] is None
    assert device.parse_wireless("only\nheaders\n")["rssi"] is None


def test_memory_and_storage_read_on_this_host():
    """Read the real /proc and statvfs — these are the shapes, not the values."""
    memory = device.memory()
    assert memory["totalMB"] and memory["totalMB"] > 0
    assert 0 <= memory["usedPercent"] <= 100
    storage = device.storage("/")
    assert storage["totalMB"] and storage["totalMB"] > 0


def test_the_assertions_above_can_fail():
    """LAW §4: each is the mutation the test above is meant to catch."""
    with pytest.raises(AssertionError):
        assert merge_flags(("--disable-features=TranslateUI",),
                           ["--disable-features=A"]) == \
            ["--disable-features=TranslateUI", "--disable-features=A"]
    with pytest.raises(AssertionError):
        assert device.decode_throttle(None)["ok"] is False
    with pytest.raises(AssertionError):
        assert Display().parse_outputs("HDMI-A-1 \"x\"\n  Enabled: no\n"
                                       )[0]["enabled"] is True


# --- what the interstitial can and cannot tell you ---------------------------

class _FakeCDP:
    """Just enough CDP to drive error_page()."""

    def __init__(self, evaluated, target_url):
        self._evaluated = evaluated
        self._target_url = target_url
        self.navigated: list[str] = []

    def evaluate(self, _script):
        return self._evaluated

    def current_url(self):
        return self._target_url

    def navigate(self, url):
        self.navigated.append(url)


def _supervisor_with(settings, cdp):
    supervisor = BrowserSupervisor(settings)
    supervisor.cdp = lambda: cdp  # type: ignore[method-assign]
    return supervisor


def test_the_failed_url_comes_from_the_target_not_the_interstitial(settings):
    """INSIDE the error page `location.href` is `chrome-error://chromewebdata/`.

    Chromium does not expose the attempted address to the document, so a
    detector that reads it hands the outage notice a dead link and a notice
    that cannot say which board is down. Measured on the first panel, 2026-09-10,
    against a genuinely unreachable board server: the wall landed on
    `outage.html?...&back=chrome-error%3A%2F%2Fchromewebdata%2F`.
    """
    cdp = _FakeCDP(
        {"code": "ERR_ADDRESS_UNREACHABLE"},
        "http://boards.invalid/alert-monitor/",
    )
    error = _supervisor_with(settings, cdp).error_page()
    assert error is not None
    assert error["url"] == "http://boards.invalid/alert-monitor/"
    assert error["code"] == "ERR_ADDRESS_UNREACHABLE"


def test_a_chrome_error_target_falls_back_to_the_address_asked_for(settings):
    """When even the target list has lost it, report what we navigated to."""
    cdp = _FakeCDP({"code": "ERR_NAME_NOT_RESOLVED"}, "chrome-error://chromewebdata/")
    supervisor = _supervisor_with(settings, cdp)
    supervisor.navigate("http://boards.invalid/alert-monitor/")
    error = supervisor.error_page()
    assert error is not None
    assert error["url"] == "http://boards.invalid/alert-monitor/"


def test_a_healthy_page_is_still_not_an_error(settings):
    """The detection must not start firing just because a URL is available."""
    cdp = _FakeCDP(None, "http://boards.invalid/alert-monitor/")
    assert _supervisor_with(settings, cdp).error_page() is None
