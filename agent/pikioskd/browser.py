"""Chromium under cage: launched, supervised, restarted, and asked what it shows.

THE AGENT OWNS THE BROWSER. It is not a control daemon bolted onto somebody
else's ``kiosk.service``; it launches the compositor and the browser itself and
is the only thing that does. That is the whole reason this exists: a wall whose
start URL lives in a shell script and whose live URL lives in a browser has two
sources of truth, they drift, and the drift is only ever discovered by looking
at the glass. Here the settings file is the configuration and the browser is
started from it, so "what is this panel pointed at" has one answer.

RESTART IS EXPONENTIALLY BACKED OFF AND THE COUNT IS PUBLISHED. A crash-looping
Chromium under a naive ``Restart=always`` reports the service as ``active``
forever — the restart COUNT is the signal, not the state, and a supervisor that
does not publish it hides its own most important finding. The backoff exists so
a panel that cannot start does not spend its SD card's remaining write cycles
finding that out.

EVERY EXIT IS CLASSIFIED, AND ONLY A CRASH IS A CRASH. Measured on four panels
over 48 hours: every browser exit was code 0 at the same second on every host —
a fleet-wide commanded restart after a dashboard deploy — and every one of them
moved the "crash" counter, so the reading the README calls the signal could
not tell an operator's button from a dying GPU. An exit the supervisor asked
for is ``commanded``; one the liveness watchdog forced is ``watchdog``; every
other exit is a ``crash``. The total is still published for continuity, and
the three parts are published beside it.

PROCESS EXIT IS NOT THE ONLY WAY A WALL GOES DARK. A Chromium wedged on the
GPU keeps its pid and paints nothing, and ``cage`` started against no output
runs for ever with DevTools never opened. The liveness watchdog restarts a
browser that has answered no DevTools request for ``browserHangSeconds`` —
GATED ON THE KERNEL REPORTING A CONNECTOR PLUGGED IN, because with nothing
connected the silence is the correct state (jrackerby/HA#771) and a restart
would loop until somebody plugged a monitor in. When one IS plugged in, the
supervisor wakes from any pending backoff at once rather than sitting out the
remainder of a five-minute wait it earned against a display that is now back.

FLAGS ARE A BASE SET PLUS THE OPERATOR'S, AND CONFLICTS RESOLVE TOWARDS THE
OPERATOR. The base set is what makes a browser a kiosk (no first-run bubbles,
no error dialogs, no update checks, a fixed profile). An operator flag with the
same ``--name=`` prefix replaces the base one rather than being appended after
it, because Chromium's behaviour on a repeated flag is not specified and
"whichever came last" is not something to build a fleet on.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from typing import Any, Callable

from .cdp import Browser, CDPError

_LOGGER = logging.getLogger(__name__)

# THERE IS EXACTLY ONE `--disable-features`, AND IT IS COMPOSED, NEVER REPEATED.
# Chromium keeps one of a repeated flag and does not say which, so a second one
# appended anywhere silently discards the first. Composing from a tuple of
# feature names makes adding one an edit to a list rather than a new flag.
DISABLED_FEATURES: tuple[str, ...] = (
    # The translate bar. Belt and braces with --disable-infobars; it has
    # NOTHING TO DO WITH THE CURSOR, whatever the comment that sat here until
    # jrackerby/kiosk-pi#9 said. See `cursor_extension_dir` below for what
    # actually addresses that.
    "TranslateUI",
)

# What makes a browser a kiosk. Each of these has a reason; none is decoration.
BASE_FLAGS: tuple[str, ...] = (
    "--kiosk",
    "--start-fullscreen",
    "--ozone-platform=wayland",
    # A kiosk has nobody to dismiss a dialog, so a dialog is a dead wall.
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--no-first-run",
    "--fast-start",
    # Chromium's own updater has no business running on a pinned fleet image,
    # and its check is a periodic outbound request from a wall.
    "--check-for-update-interval=31536000",
    # Without this Chromium blocks on an absent keyring and never paints.
    "--password-store=basic",
    # A monitor board plays its own audio cues with no user gesture available.
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=" + ",".join(DISABLED_FEATURES),
)

# THE HIDE-CURSOR EXTENSION, WRITTEN BY THE AGENT RATHER THAN BY THE INSTALLER.
#
# The fault: these panels have no pointer device, and Chromium's Ozone/Wayland
# cursor path needs a `wl_pointer` enter serial that never arrives, so whatever
# cursor was drawn at launch stays where it is — a default arrow stranded on a
# wall board for ever. `cage` has no cursor-hide option and there is no Chromium
# flag for it; setting `cursor: none` from inside the page is what makes
# Chromium commit a null cursor surface, which is why the fix is an extension
# and not a switch.
#
# 0.x SHIPPED THIS AS FILES THE INSTALLER LAID DOWN UNDER /home/kiosk, and
# nothing ever checked they were still there or still loaded. The agent writes
# them itself, on every launch, into its own StateDirectory: there is then no
# drift between what the installer wrote and what the browser is told to load,
# a reimaged host cannot lose it, and it cannot land under /home, which
# ProtectHome=read-only makes unwritable anyway.
#
# `--load-extension` IS BEING WITHDRAWN, AND THIS FLEET IS NOT AFFECTED — the
# distinction is branded vs unbranded, not the version number, and getting it
# backwards either way writes a wrong version gate into a header. GOOGLE-BRANDED
# Chrome restricted the switch at 137 and removed it, with its
# `DisableLoadExtensionCommandLineSwitch` escape hatch, at 142. Unbranded
# Chromium — which is what Raspberry Pi OS packages, and what
# `chromiumBinary` defaults to — keeps it. Measured on the live fleet: all four
# panels report Chromium 152.0.7977.82 from the distribution package. If a panel
# is ever pointed at a branded build this stops working SILENTLY, which is why
# the agent logs the directory it loaded from.
#
# WHAT IT DOES NOT COVER, stated rather than discovered later: a content script
# cannot reach `chrome-error://chromewebdata`, so the cursor is not hidden on
# Chromium's own network-error interstitial. The agent navigates away from that
# page rather than living on it, so the exposure is the seconds before the
# outage page loads — not a wall's steady state.
CURSOR_EXTENSION_FILES: dict[str, str] = {
    "manifest.json": json.dumps(
        {
            "manifest_version": 3,
            "name": "pikioskd hide cursor",
            "version": "1.0",
            "description": (
                "Hides the pointer on a panel that has no pointer device."
            ),
            "content_scripts": [
                {
                    "matches": ["<all_urls>"],
                    "css": ["hide-cursor.css"],
                    "run_at": "document_start",
                    "all_frames": True,
                }
            ],
        },
        indent=2,
    )
    + "\n",
    # `!important` because a board may set its own cursor, and on a panel with
    # no pointer every one of those is wrong.
    "hide-cursor.css": "*, *::before, *::after { cursor: none !important; }\n",
}


def _prefer_browser_for_oom(pid: int) -> None:
    """Put the browser ahead of the agent in the kernel's OOM ordering.

    The unit runs the agent at ``OOMScoreAdjust=-500`` so that when a Pi runs
    out of memory the kernel takes a Chromium renderer and not the process
    that would restart it. A child inherits its parent's adjustment, so the
    browser is moved back to 0 here — an unprivileged process may RAISE a
    child's score, never lower it, which is why the direction is this way
    round and why nothing needs sudo. Written from the parent after fork,
    before cage has had time to fork Chromium; a lost race costs nothing, the
    whole tree simply shares the agent's score and the kernel still picks the
    largest resident set, which is a renderer.
    """
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("0")
    except OSError as err:
        _LOGGER.debug("oom_score_adj not reset for %s: %s", pid, err)


def merge_flags(base: tuple[str, ...] | list[str], extra: list[str]) -> list[str]:
    """Operator flags override base flags of the same name; order is stable.

    Matched on the part BEFORE the first ``=``, so ``--disable-features=A,B``
    from an operator replaces the base ``--disable-features=TranslateUI``
    rather than sitting beside it — Chromium keeps only one and does not say
    which, so the merge has to be explicit here.
    """
    def name_of(flag: str) -> str:
        return flag.split("=", 1)[0]

    overridden = {name_of(flag) for flag in extra}
    merged = [flag for flag in base if name_of(flag) not in overridden]
    merged.extend(extra)
    return merged


class BrowserSupervisor:
    """Owns the cage+Chromium process and the DevTools conversation with it."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._last_requested_url: str = ""
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._restart_count = 0
        self._crash_count = 0
        self._commanded_count = 0
        self._watchdog_count = 0
        self._last_start: float | None = None
        self._last_exit_code: int | None = None
        self._last_exit_reason: str | None = None
        # Set BEFORE the process is signalled, read when it exits, so the
        # supervision loop can tell an exit it caused from one it did not.
        self._exit_expected: str | None = None
        self._pending_url: str | None = None
        self._on_started: list[Callable[[], None]] = []
        # Liveness. `_last_responsive` is the last time DevTools answered;
        # `_display_connected` is the kernel's last word on the connector, and
        # a reconnect resets the hang clock and cuts a pending backoff short.
        self._last_responsive: float | None = None
        self._display_connected: bool | None = None
        self._display_reconnected_at: float | None = None
        self._wake = threading.Event()

    # --- lifecycle ----------------------------------------------------------

    @property
    def restart_count(self) -> int:
        """Every exit that led to a relaunch, whatever caused it."""
        return self._restart_count

    @property
    def crash_count(self) -> int:
        """Exits nobody asked for. THE signal; the total above is not."""
        return self._crash_count

    @property
    def commanded_restart_count(self) -> int:
        return self._commanded_count

    @property
    def watchdog_restart_count(self) -> int:
        return self._watchdog_count

    @property
    def last_exit_code(self) -> int | None:
        return self._last_exit_code

    @property
    def last_exit_reason(self) -> str | None:
        """``commanded``, ``watchdog`` or ``crash`` — or None before any exit."""
        return self._last_exit_reason

    @property
    def display_connected(self) -> bool | None:
        return self._display_connected

    @property
    def running_since(self) -> float | None:
        return self._last_start

    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def cdp(self) -> Browser:
        return Browser(port=int(self._settings.get("cdpPort")))

    def start(self) -> None:
        """Start the supervision thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._supervise, name="browser-supervisor", daemon=True
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self._terminate(reason="commanded")
        thread = self._thread
        if thread is not None:
            thread.join(timeout=15)

    def restart(self, url: str | None = None) -> None:
        """Kill the browser; the supervision loop brings it back.

        ``url`` overrides the start URL for the NEXT launch only. It is not
        written to settings: a temporary redirect that silently became the
        panel's permanent configuration is the exact confusion this agent was
        built to remove.
        """
        with self._lock:
            self._pending_url = url
        self._terminate(reason="commanded")

    def recover(self, why: str) -> None:
        """Restart a browser the watchdog has judged dead while still running.

        Distinct from :meth:`restart` only in how the exit is counted: a
        forced recovery is the agent's own finding about the browser, not an
        operator's instruction, and the two must not share a counter or the
        crash signal disappears into the deploy noise all over again.
        """
        _LOGGER.warning("restarting the browser: %s", why)
        self._terminate(reason="watchdog")

    def _terminate(self, reason: str | None = None) -> None:
        with self._lock:
            process = self._process
            if reason is not None:
                self._exit_expected = reason
        if process is None or process.poll() is not None:
            return
        # SIGTERM to the whole process group. cage forks Chromium, and Chromium
        # forks a zygote and a renderer per tab, so signalling the leader alone
        # reliably leaves orphans holding the DRM device — after which the next
        # cage cannot take it and the wall stays black with no error anybody
        # sees.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _LOGGER.warning("browser did not exit on SIGTERM; killing")
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()

    # --- launching ----------------------------------------------------------

    def cursor_extension_dir(self) -> str:
        """Where the hide-cursor extension lives — beside the profile.

        Under the profile's parent rather than inside it: Chromium rewrites the
        profile directory as it pleases, and an extension it is being asked to
        load from there is a directory the loader and the profile writer both
        own.
        """
        profile = str(self._settings.get("chromiumProfileDir"))
        return os.path.join(os.path.dirname(profile) or ".", "hide-cursor")

    def write_cursor_extension(self) -> str | None:
        """Materialise the extension. Returns its directory, or None on failure.

        A FAILURE HERE IS NOT FATAL AND IS NOT SILENT. A wall with a visible
        cursor is a blemish; a wall that will not start is an outage, so an
        unwritable state directory costs the cursor fix and nothing else. It is
        logged as a warning because it needs an edit — nobody can wait it out.
        """
        directory = self.cursor_extension_dir()
        try:
            os.makedirs(directory, exist_ok=True)
            for name, content in CURSOR_EXTENSION_FILES.items():
                path = os.path.join(directory, name)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
        except OSError as err:
            _LOGGER.warning(
                "cannot write the hide-cursor extension to %s: %s. The panel "
                "will start with a visible pointer.", directory, err
            )
            return None
        # Logged at INFO on every launch, because the one failure mode left is
        # a branded Chromium ignoring the switch without saying so: the
        # directory named here and no cursor on the glass is the discriminator.
        _LOGGER.info("loading the hide-cursor extension from %s", directory)
        return directory

    def command(self, url: str) -> list[str]:
        settings = self._settings
        flags = merge_flags(BASE_FLAGS, list(settings.get("chromiumFlags")))
        if settings.get("hideCursor"):
            directory = self.write_cursor_extension()
            if directory is not None:
                flags = merge_flags(flags, [f"--load-extension={directory}"])
        flags = merge_flags(flags, [
            f"--remote-debugging-port={int(settings.get('cdpPort'))}",
            f"--user-data-dir={settings.get('chromiumProfileDir')}",
            f"--disk-cache-dir={settings.get('chromiumCacheDir')}",
        ])
        if not settings.get("kioskMode"):
            flags = [flag for flag in flags if flag != "--kiosk"]
        return [
            settings.get("cageBinary"), "-d", "--",
            settings.get("chromiumBinary"), *flags, url,
        ]

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        # DEFER, NEVER OVERWRITE. An XDG_RUNTIME_DIR already in the environment
        # was put there by whatever started this process and is authoritative;
        # the setting is the fallback for a bare start.
        env.setdefault("XDG_RUNTIME_DIR", str(self._settings.get("xdgRuntimeDir")))
        env.setdefault("WLR_BACKENDS", "drm,libinput")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        return env

    def _launch(self, url: str) -> None:
        argv = self.command(url)
        binary = argv[0]
        if shutil.which(binary) is None and not os.path.exists(binary):
            raise FileNotFoundError(
                f"{binary} is not installed; the wall cannot start"
            )
        runtime_dir = self.environment()["XDG_RUNTIME_DIR"]
        os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        _LOGGER.info("launching: %s", shlex.join(argv))
        with self._lock:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv,
                env=self.environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own process group, so _terminate can signal the whole
                # tree without also signalling this agent.
                start_new_session=True,
            )
            self._last_start = time.time()
            self._last_exit_code = None
            self._last_responsive = None
            self._exit_expected = None
        _prefer_browser_for_oom(self._process.pid)
        for callback in self._on_started:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a hook must not kill the wall
                _LOGGER.exception("browser start hook failed")

    def add_start_hook(self, callback: Callable[[], None]) -> None:
        self._on_started.append(callback)

    def _supervise(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            with self._lock:
                url = self._pending_url or str(self._settings.get("startURL"))
                self._pending_url = None
            try:
                self._launch(url)
            except (OSError, FileNotFoundError) as err:
                _LOGGER.error("cannot launch browser: %s", err)
                # A LAUNCH FAILURE BACKS OFF LIKE A CRASH DOES. Retrying a
                # missing binary in a tight loop writes megabytes of journal an
                # hour to an SD card that is already the fleet's weakest part.
                backoff = min(max(backoff * 2, 5.0), 300.0)
                self._wait(backoff)
                continue

            with self._lock:
                process = self._process
            code = process.wait() if process else None
            with self._lock:
                self._last_exit_code = code
                self._process = None
                reason = self._exit_expected or "crash"
                self._exit_expected = None
            if self._stop.is_set():
                return
            self._record_exit(reason)
            if reason != "crash":
                # An exit this agent asked for is relaunched on the configured
                # delay and never compounds the backoff: a fleet deploy that
                # restarts every wall twice in ten seconds is not a browser
                # that cannot start, and treating it as one would leave the
                # second launch waiting out a penalty the browser did not earn.
                backoff = float(self._settings.get("browserRestartBackoffSeconds"))
                _LOGGER.info("browser exited with %s (%s restart #%d); "
                             "relaunching in %.0fs", code, reason,
                             self._restart_count, backoff)
                self._wait(backoff)
                continue
            if not self._settings.get("browserRestartOnCrash"):
                _LOGGER.error(
                    "browser exited (%s) and browserRestartOnCrash is off; "
                    "the wall stays dark until something restarts it", code
                )
                return
            ran_for = time.time() - (self._last_start or time.time())
            configured = float(self._settings.get("browserRestartBackoffSeconds"))
            if ran_for > 60:
                # A browser that stayed up for a minute did not fail to start;
                # whatever killed it is not a startup problem, so the backoff
                # resets rather than compounding across a week of uptime.
                backoff = configured
            else:
                backoff = min(max(backoff * 2, configured or 1.0), 300.0)
            _LOGGER.warning(
                "browser exited with %s after %.0fs (crash #%d, restart #%d); "
                "restarting in %.0fs", code, ran_for, self._crash_count,
                self._restart_count, backoff
            )
            self._wait(backoff)

    def _record_exit(self, reason: str) -> None:
        with self._lock:
            self._restart_count += 1
            self._last_exit_reason = reason
            if reason == "commanded":
                self._commanded_count += 1
            elif reason == "watchdog":
                self._watchdog_count += 1
            else:
                self._crash_count += 1

    def _wait(self, seconds: float) -> None:
        """Sleep out a backoff, unless something worth waking for happens.

        Two things end it early: shutdown, and a display coming back. The
        second is why this is not ``self._stop.wait``: a wall whose monitor was
        off long enough for cage to give up five times has earned a 300s
        backoff against a display that no longer exists, and sitting it out
        after the display returns is five minutes of black glass for nothing.
        """
        self._wake.clear()
        self._wake.wait(seconds)

    # --- liveness -----------------------------------------------------------

    def note_display(self, connected: bool | None, now: float | None = None) -> None:
        """Record the kernel's connector status; react to a reconnect.

        ``None`` (unreadable) is recorded and otherwise ignored — it neither
        arms nor disarms anything, so a host with no readable DRM behaves
        exactly as it did before this reading existed.
        """
        now = time.time() if now is None else now
        with self._lock:
            previous = self._display_connected
            self._display_connected = connected
        if connected is True and previous is False:
            _LOGGER.info("display output reconnected; the browser gets a fresh "
                         "%ss to answer before it is restarted",
                         self._settings.get("browserHangSeconds"))
            with self._lock:
                self._display_reconnected_at = now
            self._wake.set()
        elif connected is False and previous is True:
            _LOGGER.info("display output disconnected; the browser will not be "
                         "restarted while nothing is plugged in")

    def responsive(self) -> bool:
        """Did DevTools answer just now. The liveness probe, nothing more."""
        try:
            self.cdp().version()
        except CDPError:
            return False
        return True

    def check_liveness(self, now: float | None = None) -> str | None:
        """Restart a running browser that has gone silent on a connected output.

        Returns the reason string when it acted, None otherwise. The clock
        runs from the LATEST of: launch, the last DevTools answer, and the
        last display reconnect — so a fresh launch, a browser that was fine a
        moment ago and a monitor just plugged back in each get the full
        ``browserHangSeconds`` before anything is concluded.

        NEVER ACTS ON A GUESS. Watchdog disabled, browser not running, or
        connector state anything but a positive ``connected`` — all return
        None. The bench host with nothing plugged in reads unresponsive for
        ever and that is correct; restarting it would be the loop this gate
        exists to refuse.
        """
        now = time.time() if now is None else now
        limit = int(self._settings.get("browserHangSeconds"))
        if limit <= 0 or not self.is_running():
            return None
        with self._lock:
            connected = self._display_connected
        if connected is not True:
            return None
        if self.responsive():
            with self._lock:
                self._last_responsive = now
            return None
        with self._lock:
            # `is None` tests, not truthiness: a clock reading of 0.0 is a
            # time, and `or` would replace it with `now` and never fire.
            anchors = [t for t in (self._last_start, self._last_responsive,
                                   self._display_reconnected_at) if t is not None]
        silent_for = now - (max(anchors) if anchors else now)
        if silent_for < limit:
            return None
        self.recover(f"no DevTools answer for {silent_for:.0f}s on a connected "
                     f"output (limit {limit}s)")
        return "watchdog"

    # --- what it is showing -------------------------------------------------

    def current_url(self) -> str | None:
        try:
            return self.cdp().current_url()
        except CDPError as err:
            _LOGGER.debug("current_url unavailable: %s", err)
            return None

    def cursor_style(self) -> str | None:
        """The cursor the live document has actually resolved to.

        THE ONE THING ABOUT THE CURSOR THAT IS READABLE FROM HOME ASSISTANT,
        and the reason it exists: a CDP screenshot is
        ``Page.captureScreenshot``, which renders the page out of Chromium's
        RENDERER compositor, while the stranded cursor is a ``wl_pointer``
        cursor surface Chromium hands to cage. The two never meet, so
        ``image.<panel>_screenshot`` is guaranteed to show no cursor whether or
        not one is on the glass — which is exactly how a screenshot sweep
        reported four walls clean while one of them was stuck
        (jrackerby/kiosk-pi#9).

        NECESSARY, NOT SUFFICIENT, AND THAT IS THE POINT. `none` proves the
        hide-cursor extension loaded and its rule applied; it does NOT prove
        the glass is clean, because a Chromium that already committed a cursor
        surface and never receives another pointer-enter can keep drawing it
        regardless. That is precisely the discriminator the 0.x fix never had:
        a wall reading `none` with a cursor still on it is the compositor-
        surface fault, and a wall reading `auto` is a fix that never arrived.
        Distinguishing those two currently costs a trip to the wall.

        Read off ``documentElement`` rather than ``body``: an interstitial and
        a board that has not painted yet both have a documentElement, and
        ``body`` is null on the former.
        """
        if not self.is_running():
            return None
        try:
            value = self.cdp().evaluate(
                "getComputedStyle(document.documentElement).cursor"
            )
        except CDPError as err:
            _LOGGER.debug("cursor_style unavailable: %s", err)
            return None
        # A non-string is a page that answered something unusable. Report
        # UNKNOWN rather than coercing, or `None` becomes the string "None"
        # and reads as a cursor nobody can look up.
        return value if isinstance(value, str) and value else None

    def navigate(self, url: str) -> None:
        # Remembered as the LAST ADDRESS ASKED FOR, so an interstitial that
        # hides the attempted url from both the document and the target list
        # can still be reported against something real. See error_page().
        self._last_requested_url = url
        self.cdp().navigate(url)

    def error_page(self) -> dict[str, Any] | None:
        """Chromium's own network-error page, if that is what is on the glass.

        Detected on ``#main-frame-error``, the container Chromium's interstitial
        always carries. The URL alone cannot tell you: a failed navigation
        leaves the ATTEMPTED url in the target list, so a wall showing "site
        cannot be reached" reports the board's address and reads healthy.

        Returns None both when the page is fine and when the browser cannot be
        asked. Those are different, and the caller treats an unreachable
        browser as its own condition rather than as a healthy page — the
        substitution below only fires on a positive detection.
        """
        script = (
            "(function(){var e=document.querySelector('#main-frame-error');"
            "if(!e)return null;var c=document.querySelector('.error-code');"
            "return {code:(c&&c.textContent||'').trim()};})()"
        )
        try:
            result = self.cdp().evaluate(script)
            if not isinstance(result, dict):
                return None
            # THE FAILED URL COMES FROM THE TARGET LIST, NOT FROM THE PAGE.
            # Inside the interstitial `location.href` is
            # `chrome-error://chromewebdata/` — Chromium does not expose the
            # attempted address to the document. The TARGET still carries it,
            # which is the same fact this method's own detection rests on. Read
            # off the page, the outage notice was handed
            # `back=chrome-error://chromewebdata/`: a dead link, and a notice
            # that cannot say which board is down. Measured on the first panel,
            # 2026-09-10, against a genuinely unreachable board server.
            target = self.cdp().current_url()
            if not target or target.startswith("chrome-error:"):
                # Nothing better available; the caller still gets the code.
                target = str(
                    self._last_requested_url
                    or self._settings.get("startURL")
                    or ""
                )
            result["url"] = target
            return result
        except CDPError:
            return None

    def error_url_for(self, template: str, failed_url: str,
                      code: str, panel: str) -> str:
        """The outage page's address, carrying what failed.

        The query contract is the one Fully Kiosk's own outage pages already
        read — ``panel``, ``back``, ``error``, ``url`` — so a wall driven by
        this agent lands on the same notice, saying the same thing, as a
        tablet driven by Fully. Reproducing a working contract costs
        nothing; inventing a second one costs every consumer of the first.

        EXISTING QUERY PARAMETERS ON THE TEMPLATE ARE PRESERVED. An operator who
        pointed errorURL at a page with its own ``?theme=dark`` should keep it,
        so the parameters are merged rather than the string being concatenated.
        """
        parts = urllib.parse.urlsplit(template)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query.update({"panel": panel, "back": failed_url, "url": failed_url})
        if code:
            query["error"] = code
        return urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, parts.path,
            urllib.parse.urlencode(query), parts.fragment,
        ))
