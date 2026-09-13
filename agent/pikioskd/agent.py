"""The agent proper: settings + browser + display, and the commands over them.

This is where a request becomes an effect. The HTTP layer above it does routing
and authentication and nothing else, so every command here is callable — and
testable — with no socket in play.

TWO TIMERS, AND THEY ARE NOT THE SAME TIMER. ``timeToScreensaverV2`` dims the
wall and shows a screensaver page; ``timeToScreenOffV2`` cuts the output. Fully
models them separately and so does this, because they answer different
questions: a monitor board that dims overnight is still a board, and one whose
output is off is a dark rectangle somebody has to walk over to. Both count from
the last *interaction*, which for a panel with no input devices means the last
command this agent received — that is the honest definition here, and it is
stated rather than left for a reader to infer from a wall that never sleeps.

EVERY ACTION THAT CHANGES THE WALL READS BACK THE THING IT CLAIMS TO HAVE
CHANGED. A command returning OK only says the call was dispatched; the reply
carries the observed state so the caller never has to believe an assertion it
could have measured.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any

from . import device
from .browser import BrowserSupervisor
from .cdp import CDPError
from .display import Display, DisplayError
from .settings import BROWSER_RESTART_KEYS, Settings
from .version import __version__

_LOGGER = logging.getLogger(__name__)

# How often the background loop looks at the wall. Short enough that an outage
# page is substituted while somebody is still walking towards the screen, long
# enough that it is not a meaningful share of a Pi 3B's CPU.
TICK_SECONDS = 5.0


class KioskAgent:
    """Everything a panel can be asked to do."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.browser = BrowserSupervisor(settings)
        self.display = self._build_display()
        self.started_at = time.time()

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_interaction = time.time()
        self._screensaver_active = False
        self._screen_off_by_timer = False
        self._overlay_text = ""
        self._error_substituted_for: str | None = None
        # When the background loop last finished a tick. The systemd watchdog
        # is fed from __main__ ONLY while this is recent, so a tick wedged
        # inside a subprocess or a socket is what stops the pings — which is
        # the one condition the watchdog exists to catch.
        self._last_tick_at: float | None = None

        settings.add_listener(self._on_settings_changed)

    # --- wiring -------------------------------------------------------------

    def _build_display(self) -> Display:
        return Display(
            output_name=str(self.settings.get("outputName")),
            xdg_runtime_dir=str(self.settings.get("xdgRuntimeDir")),
        )

    def _on_settings_changed(self, changed: set[str]) -> None:
        if {"outputName", "xdgRuntimeDir"} & changed:
            self.display = self._build_display()
        if "rotation" in changed:
            try:
                self.display.set_rotation(str(self.settings.get("rotation")))
            except DisplayError as err:
                _LOGGER.error("rotation not applied: %s", err)
        if "screenBrightness" in changed and not self._screensaver_active:
            self._apply_brightness(int(self.settings.get("screenBrightness")))
        if BROWSER_RESTART_KEYS & changed:
            _LOGGER.info("restarting browser for changed settings: %s",
                         ", ".join(sorted(BROWSER_RESTART_KEYS & changed)))
            self.browser.restart()

    def start(self) -> None:
        self.browser.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-loop",
                                        daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.browser.shutdown()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    def touch(self) -> None:
        """Record an interaction. Called by the HTTP layer on every command.

        A COMMAND IS AN INTERACTION, INCLUDING A POLL — and that is a real
        limitation, stated rather than hidden: an integration polling this
        agent every 30 seconds would hold the screensaver off forever. The HTTP
        layer therefore does NOT touch on read-only commands; only commands
        that change something count. Getting this backwards produces a wall
        that never sleeps and a settings page that looks correct.
        """
        with self._lock:
            self._last_interaction = time.time()

    # --- background loop ----------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the loop must outlive any one tick
                _LOGGER.exception("agent tick failed")

    def _tick(self) -> None:
        self._check_display()
        self._check_error_page()
        self._check_idle_timers()
        with self._lock:
            self._last_tick_at = time.time()

    @property
    def last_tick_at(self) -> float | None:
        with self._lock:
            return self._last_tick_at

    def loop_healthy(self, now: float | None = None,
                     stale_after: float = TICK_SECONDS * 6) -> bool:
        """Is the background loop alive AND recently through a tick.

        Both halves matter. A dead thread is obvious; a live thread wedged
        inside one tick is not, and `is_alive()` says yes for it for ever.
        Six ticks of slack covers a slow Pi 3B with a screenshot in flight.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        last = self.last_tick_at
        if last is None:
            # Not through a first tick yet: healthy for as long as a first
            # tick could reasonably take, judged from start.
            last = self.started_at
        now = time.time() if now is None else now
        return (now - last) < stale_after

    def _check_display(self) -> None:
        """Feed the connector state to the supervisor and let it judge.

        The read is sysfs, a few file opens; the judgement lives in the
        supervisor because it owns the launch clock the decision depends on.
        """
        connected = device.display_connected(device.drm_connectors())
        self.browser.note_display(connected)
        self.browser.check_liveness()

    def _check_error_page(self) -> None:
        """Substitute the outage page when Chromium is showing its own error.

        ONCE PER FAILED URL, not once per tick. Re-navigating every five
        seconds would reload the outage page continuously, which flickers, and
        would make the substitution indistinguishable in a log from a panel
        crash-looping. The latch clears as soon as the wall is somewhere else.
        """
        template = str(self.settings.get("errorURL"))
        if not template:
            return
        error = self.browser.error_page()
        if error is None:
            with self._lock:
                self._error_substituted_for = None
            return
        failed = str(error.get("url") or "")
        with self._lock:
            if self._error_substituted_for == failed:
                return
            self._error_substituted_for = failed
        target = self.browser.error_url_for(
            template, failed, str(error.get("code") or ""),
            str(self.settings.get("deviceName")) or socket.gethostname(),
        )
        _LOGGER.warning("substituting outage page for %s (%s)", failed,
                        error.get("code"))
        try:
            self.browser.navigate(target)
        except CDPError as err:
            _LOGGER.error("could not show the outage page: %s", err)

    def _check_idle_timers(self) -> None:
        with self._lock:
            idle = time.time() - self._last_interaction
            screensaver_after = int(self.settings.get("timeToScreensaverV2"))
            screen_off_after = int(self.settings.get("timeToScreenOffV2"))
            screensaver_active = self._screensaver_active
            screen_off = self._screen_off_by_timer

        if screen_off_after and idle >= screen_off_after and not screen_off:
            _LOGGER.info("screen off after %.0fs idle", idle)
            self._set_screen(False, by_timer=True)
        elif screensaver_after and idle >= screensaver_after and not screensaver_active:
            _LOGGER.info("screensaver on after %.0fs idle", idle)
            self.start_screensaver()

    # --- screen -------------------------------------------------------------

    def _apply_brightness(self, level: int) -> int | None:
        try:
            return self.display.set_brightness(level)
        except DisplayError as err:
            # Not an error on a fleet driving HDMI monitors — there is simply
            # no backlight to write. Logged at debug so it does not fill a
            # journal every time a screensaver starts.
            _LOGGER.debug("brightness not applied: %s", err)
            return None

    def _set_screen(self, on: bool, by_timer: bool = False) -> dict[str, Any]:
        instrument = self.display.set_power(on)
        with self._lock:
            self._screen_off_by_timer = (not on) and by_timer
            if on:
                self._screensaver_active = False
        if on:
            self._apply_brightness(int(self.settings.get("screenBrightness")))
        return {"screenOn": on, "instrument": instrument}

    def screen_on(self) -> dict[str, Any]:
        return self._set_screen(True)

    def screen_off(self) -> dict[str, Any]:
        return self._set_screen(False)

    def set_brightness(self, level: int) -> dict[str, Any]:
        """Set brightness AND persist it, because a slider that forgets is a bug.

        Fully's brightness is a setting, not a transient, and an operator who
        dims a wall expects it dim after a reboot. Persisting through the
        settings map also means the change goes down the one write path that
        validates, so a 0-255 range is enforced in exactly one place.
        """
        self.settings.set("screenBrightness", level)
        raw = self._apply_brightness(level)
        if raw is None:
            raise DisplayError(
                "brightness stored but not applied: this display has no "
                "software-controllable backlight"
            )
        return {"screenBrightness": level, "rawValue": raw}

    # --- screensaver --------------------------------------------------------

    def start_screensaver(self) -> dict[str, Any]:
        """Dim, and show the screensaver page if one is configured.

        WITH NO WALLPAPER URL THIS IS A DIM, NOT A BLANK. Navigating away from
        the board to show black would lose whatever the board was doing and
        make stopping the screensaver a full reload; dimming leaves the page
        mounted and live, which is what a monitor board wants at 2am.
        """
        wallpaper = str(self.settings.get("screensaverWallpaperURL"))
        applied = self._apply_brightness(
            int(self.settings.get("screensaverBrightness"))
        )
        navigated = False
        if wallpaper:
            try:
                self.browser.navigate(wallpaper)
                navigated = True
            except CDPError as err:
                _LOGGER.error("screensaver page not shown: %s", err)
        with self._lock:
            self._screensaver_active = True
        return {"screensaverOn": True, "dimmed": applied is not None,
                "navigated": navigated}

    def stop_screensaver(self) -> dict[str, Any]:
        with self._lock:
            was_active = self._screensaver_active
            self._screensaver_active = False
            self._last_interaction = time.time()
        self._apply_brightness(int(self.settings.get("screenBrightness")))
        if was_active and str(self.settings.get("screensaverWallpaperURL")):
            try:
                self.browser.navigate(str(self.settings.get("startURL")))
            except CDPError as err:
                _LOGGER.error("could not leave the screensaver page: %s", err)
        return {"screensaverOn": False}

    @property
    def screensaver_active(self) -> bool:
        with self._lock:
            return self._screensaver_active

    # --- browser ------------------------------------------------------------

    def load_url(self, url: str) -> dict[str, Any]:
        """Navigate now, without changing what the panel comes back to.

        DELIBERATELY TRANSIENT, matching Fully's ``loadURL``. Persisting it here
        would mean an automation that flips a wall to a camera feed for thirty
        seconds has silently re-provisioned the panel — discovered at the next
        reboot, by which time nobody connects the two. ``setStringSetting``
        with ``startURL`` is how a caller says it meant it permanently.
        """
        self.stop_screensaver()
        self.browser.navigate(url)
        return {"requestedURL": url, "currentURL": self.browser.current_url()}

    def load_start_url(self) -> dict[str, Any]:
        return self.load_url(str(self.settings.get("startURL")))

    def restart_browser(self) -> dict[str, Any]:
        before = self.browser.restart_count
        self.browser.restart()
        return {"restartRequested": True, "restartCountBefore": before,
                "countedAs": "commanded"}

    def clear_cache(self) -> dict[str, Any]:
        """Flush the HTTP and code caches through the browser itself.

        NOT ``rm -rf`` ON THE CACHE DIRECTORY, and not a browser restart. The
        directory is only where the cache happens to live today, so a path that
        moved makes the delete a silent no-op that still exits 0 and still
        reports success. ``Network.clearBrowserCache`` asks the component that
        owns the cache, needs no privileges, and does not take the wall down to
        do it.
        """
        self.browser.cdp().clear_cache()
        return {"cacheCleared": True}

    def clear_cookies(self) -> dict[str, Any]:
        self.browser.cdp().clear_cookies()
        return {"cookiesCleared": True}

    def screenshot(self) -> bytes:
        return self.browser.cdp().screenshot_png()

    def to_foreground(self) -> dict[str, Any]:
        self.browser.cdp().bring_to_front()
        return {"foreground": True}

    def set_overlay(self, text: str) -> dict[str, Any]:
        """A full-screen message drawn over whatever the board is showing.

        THE PAGE UNDERNEATH IS UNTOUCHED — it keeps its socket, its timers and
        its state, so clearing the overlay reveals a live board rather than one
        that has to reload. That is the property that makes this usable for an
        alert: navigating to a notice page and back costs the board's entire
        session, and a directive surface cannot afford to be reloading when it
        is needed.

        The overlay is injected into the page rather than composited by the
        agent because there is no compositor surface available above a
        fullscreen Chromium under cage. It therefore does NOT survive a
        navigation, and the agent re-asserts it after one; a caller that needs
        it to persist must re-issue it, which the reply says.
        """
        script = (
            "(function(t){var id='pikioskd-overlay';var e=document.getElementById(id);"
            "if(!t){if(e)e.remove();return false;}"
            "if(!e){e=document.createElement('div');e.id=id;"
            "e.setAttribute('style','position:fixed;inset:0;z-index:2147483647;"
            "display:flex;align-items:center;justify-content:center;"
            "background:rgba(0,0,0,0.92);color:#fff;font:600 5vmin/1.3 system-ui,"
            "sans-serif;text-align:center;padding:6vmin;white-space:pre-wrap;');"
            "document.documentElement.appendChild(e);}"
            "e.textContent=t;return true;})(" + _js_string(text) + ")"
        )
        with self._lock:
            self._overlay_text = text
        shown = bool(self.browser.cdp().evaluate(script))
        return {"overlay": text, "shown": shown,
                "note": "an overlay does not survive a navigation"}

    # --- telemetry ----------------------------------------------------------

    def device_info(self) -> dict[str, Any]:
        """The single poll the integration lives on.

        ONE CALL, EVERYTHING. A coordinator that has to make six requests to
        build one picture reports six independent failure modes and gets a
        torn read whenever the wall changes mid-poll. Every field here is read
        within one tick of every other.
        """
        info = device.base_info()
        screen = self.display.state()
        connectors = device.drm_connectors()
        running = self.browser.is_running()
        current_url = self.browser.current_url() if running else None
        # One extra CDP round trip on the poll, and it buys the only
        # cursor reading that exists off-device (browser.cursor_style).
        # Skipped entirely when the browser is down, where it would just be a
        # guaranteed timeout on every poll of a panel already known to be dark.
        cursor_style = self.browser.cursor_style() if running else None
        with self._lock:
            overlay = self._overlay_text
            screensaver = self._screensaver_active
            idle = time.time() - self._last_interaction
        info.update({
            "agentVersion": __version__,
            "agentUptimeSeconds": round(time.time() - self.started_at, 1),
            "deviceName": str(self.settings.get("deviceName"))
                          or info.get("hostname"),
            "browserRunning": running,
            # THE TOTAL AND ITS PARTS. `browserRestartCount` keeps its 1.0.0
            # meaning (every relaunch) so nothing reading it moves; the crash
            # count is the one that answers "is this browser dying".
            "browserRestartCount": self.browser.restart_count,
            "browserCrashCount": self.browser.crash_count,
            "browserCommandedRestartCount": self.browser.commanded_restart_count,
            "browserWatchdogRestartCount": self.browser.watchdog_restart_count,
            "browserLastExitCode": self.browser.last_exit_code,
            "browserLastExitReason": self.browser.last_exit_reason,
            "browserResponsive": (current_url is not None) if running else False,
            "browserHangSeconds": int(self.settings.get("browserHangSeconds")),
            "browserVersion": device.chromium_version(
                str(self.settings.get("chromiumBinary"))
            ),
            "currentURL": current_url,
            "cursorStyle": cursor_style,
            "hideCursor": bool(self.settings.get("hideCursor")),
            "startURL": str(self.settings.get("startURL")),
            "screenOn": screen["on"],
            "screenBrightness": int(self.settings.get("screenBrightness")),
            "screenRawBrightness": screen["brightness"],
            "screenBrightnessMax": screen["brightnessMax"],
            "screenInstrument": screen["instrument"],
            "displayOutput": screen["output"],
            # The kernel's word, beside the compositor's: a connector list and
            # whether any is plugged in. The compositor reports what it is
            # driving, which with nothing connected is nothing — and that is
            # indistinguishable, from the compositor alone, from a compositor
            # that failed to start (jrackerby/HA#771).
            "displayConnected": device.display_connected(connectors),
            "displayConnectors": connectors,
            "displayMake": screen["make"],
            "displayModel": screen["model"],
            "resolution": screen["resolution"],
            "orientation": screen["orientation"],
            "screensaverOn": screensaver,
            "kioskMode": bool(self.settings.get("kioskMode")),
            "maintenanceMode": bool(self.settings.get("maintenanceMode")),
            "overlayMessage": overlay,
            "idleSeconds": round(idle, 1),
        })
        return info


def _js_string(value: str) -> str:
    """A JavaScript string literal for ``value``.

    Built with ``json.dumps`` rather than by quoting, because an overlay message
    is arbitrary operator text that will eventually contain a quote, a newline
    or a ``</script>``. Injecting it by concatenation makes the message able to
    terminate the expression it is embedded in — the message is data and has to
    arrive as data.
    """
    return json.dumps(value)
