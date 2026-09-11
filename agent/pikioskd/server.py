"""The remote-admin HTTP surface — Fully Kiosk's command vocabulary, on a Pi.

SHAPE, DELIBERATELY COPIED: ``GET /?cmd=<name>&password=<pw>&...`` answering
JSON with a ``status`` of ``OK`` or ``Error``. An operator who knows Fully's
:2323 surface can drive this one from a browser address bar on the first day,
and an existing note, bookmark or curl one-liner written for Fully keeps its
shape. The commands are named the same where they mean the same thing, and
deliberately NOT named the same where they do not — there is no
``getCamshot`` here answering something that is not a camera.

WHAT IS FIXED RATHER THAN COPIED:

- **Everything answers a real status code.** Kiosker answers 200 to everything,
  an undefined function included, putting the verdict in the body — so a status
  check reads a guessed command name as live and a fleet audit passes over
  devices that understood nothing. Here an unknown command is 404, a bad
  password is 401, a bad argument is 400, and a device-side failure is 502.
  The body still carries the detail; the code carries the verdict.
- **Types are honest.** See ``settings.py``: an int reads back an int.
- **The password is compared in constant time** and is never echoed, never
  logged, and never included in a redirect or an error message.

WHY GET FOR MUTATING COMMANDS. It is what Fully does and what every consumer
already expects, and this surface is loopback-and-LAN only behind a shared
secret rather than a cookie, so it carries no ambient authority for a
cross-site request to borrow. POST is accepted for the same commands so a
caller that prefers it is not forced into a GET.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import socketserver
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .agent import KioskAgent
from .cdp import CDPError
from .display import DisplayError
from .settings import (
    BOOLEAN_KEYS,
    BROWSER_RESTART_KEYS,
    INT_KEYS,
    SettingError,
)
from .version import __version__

_LOGGER = logging.getLogger(__name__)

# Query parameters that must never reach a log. `password` is the shared
# admin secret; `value` can carry it, because setting the password is
# itself a command.
_SECRET_PARAMS = frozenset({"password", "remoteAdminPassword"})
_SECRET_IN_TEXT = re.compile(r"(password=)[^&\s\"]*", re.IGNORECASE)

# Commands that only read. They do not reset the idle clock, so a Home
# Assistant coordinator polling deviceInfo every 30 seconds cannot hold the
# screensaver off forever — which is what would happen if every request counted
# as an interaction, and it would look exactly like a broken timer setting.
READ_ONLY_COMMANDS = frozenset({
    "deviceInfo", "listSettings", "getCurrentURL", "getScreenshot", "status",
})


class CommandError(Exception):
    """A command failed in a way the caller should be told about verbatim."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


def _require(params: dict[str, str], name: str) -> str:
    value = params.get(name)
    if value is None or value == "":
        raise CommandError(f"missing required parameter: {name}")
    return value


class CommandTable:
    """Name -> handler. Kept separate from the HTTP handler so it is testable."""

    def __init__(self, agent: KioskAgent) -> None:
        self.agent = agent
        self._table: dict[str, Callable[[dict[str, str]], Any]] = {
            # --- read -------------------------------------------------------
            "status": self._status,
            "deviceInfo": lambda _p: self.agent.device_info(),
            "listSettings": lambda _p: self.agent.settings.as_dict(),
            "getCurrentURL": lambda _p: {"url": self.agent.browser.current_url()},
            "getScreenshot": self._screenshot,
            # --- settings ---------------------------------------------------
            "setStringSetting": self._set_string_setting,
            "setBooleanSetting": self._set_boolean_setting,
            "setIntSetting": self._set_int_setting,
            # --- browser ----------------------------------------------------
            "loadURL": lambda p: self.agent.load_url(_require(p, "url")),
            "loadStartURL": lambda _p: self.agent.load_start_url(),
            "restartApp": lambda _p: self.agent.restart_browser(),
            "toForeground": lambda _p: self.agent.to_foreground(),
            "clearCache": lambda _p: self.agent.clear_cache(),
            "clearCookies": lambda _p: self.agent.clear_cookies(),
            "setOverlayMessage": lambda p: self.agent.set_overlay(
                p.get("text", "")
            ),
            # --- screen -----------------------------------------------------
            "screenOn": lambda _p: self.agent.screen_on(),
            "screenOff": lambda _p: self.agent.screen_off(),
            "setBrightness": self._set_brightness,
            "startScreensaver": lambda _p: self.agent.start_screensaver(),
            "stopScreensaver": lambda _p: self.agent.stop_screensaver(),
            # --- device -----------------------------------------------------
            "rebootDevice": self._reboot,
        }

    def names(self) -> list[str]:
        return sorted(self._table)

    def dispatch(self, command: str, params: dict[str, str]) -> Any:
        handler = self._table.get(command)
        if handler is None:
            raise CommandError(
                f"unknown command: {command}. Known commands: "
                + ", ".join(self.names()),
                HTTPStatus.NOT_FOUND,
            )
        if command not in READ_ONLY_COMMANDS:
            self.agent.touch()
        try:
            return handler(params)
        except SettingError as err:
            raise CommandError(str(err), HTTPStatus.BAD_REQUEST) from err
        except OSError as err:
            # The agent is running; its STORAGE is not co-operating. Named
            # explicitly rather than falling through to the generic handler,
            # because "internal error" sends an operator to the agent's code
            # when the answer is a full or read-only filesystem — and because
            # the setting has been rolled back, which the caller needs told.
            raise CommandError(
                f"the change was rejected and NOT saved: {err}. The setting "
                "has been rolled back in memory, so what this agent reports is "
                "still what is on disk.",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from err
        except (CDPError, DisplayError) as err:
            # 502, not 500: the agent is fine and something it depends on is
            # not. That distinction is the difference between "restart the
            # agent" and "the browser is down", and collapsing it sends every
            # operator to the wrong place first.
            raise CommandError(str(err), HTTPStatus.BAD_GATEWAY) from err

    # --- handlers -----------------------------------------------------------

    def _status(self, _params: dict[str, str]) -> dict[str, Any]:
        """A cheap liveness answer that touches no hardware.

        Exists so a health check can distinguish "the agent is up" from "the
        agent is up and the browser and screen are answering", which
        ``deviceInfo`` conflates by design — it reads everything, so it is slow
        and it fails when any one instrument does.
        """
        return {
            "agentVersion": __version__,
            "browserRunning": self.agent.browser.is_running(),
            "browserRestartCount": self.agent.browser.restart_count,
            "uptimeSeconds": round(time.time() - self.agent.started_at, 1),
        }

    def _screenshot(self, _params: dict[str, str]) -> tuple[bytes, str]:
        return self.agent.screenshot(), "image/png"

    def _set_string_setting(self, params: dict[str, str]) -> dict[str, Any]:
        key = _require(params, "key")
        value = params.get("value", "")
        # ACCEPTED FOR ANY KEY, INCLUDING TYPED ONES, and coerced rather than
        # refused. Fully answers 200 and silently no-ops when a bool is written
        # through the string setter, so callers learned to write, re-read and
        # fall back to the other setter. Coercing here means the obvious call
        # works and the reply says what was actually stored.
        changed = self.agent.settings.set(key, value)
        return self._setting_reply(key, changed)

    def _set_boolean_setting(self, params: dict[str, str]) -> dict[str, Any]:
        key = _require(params, "key")
        if key not in BOOLEAN_KEYS:
            raise CommandError(
                f"{key} is not a boolean setting; use setStringSetting or "
                "setIntSetting"
            )
        changed = self.agent.settings.set(key, _require(params, "value"))
        return self._setting_reply(key, changed)

    def _set_int_setting(self, params: dict[str, str]) -> dict[str, Any]:
        key = _require(params, "key")
        if key not in INT_KEYS:
            raise CommandError(f"{key} is not an integer setting")
        changed = self.agent.settings.set(key, _require(params, "value"))
        return self._setting_reply(key, changed)

    def _setting_reply(self, key: str, changed: set[str]) -> dict[str, Any]:
        """Read the value back and say whether a restart is still owed.

        The caller never has to believe the write: the stored value is in the
        reply. ``restartRequired`` is false because the agent has ALREADY
        restarted the browser for the keys that need it — it is reported so a
        caller can explain a wall that just went blank rather than diagnose it.
        """
        return {
            "key": key,
            "value": self.agent.settings.get(key),
            "changed": key in changed,
            "browserRestarted": key in (BROWSER_RESTART_KEYS & changed),
        }

    def _set_brightness(self, params: dict[str, str]) -> dict[str, Any]:
        raw = params.get("level", params.get("value"))
        if raw is None:
            raise CommandError("missing required parameter: level")
        try:
            level = int(raw)
        except ValueError as err:
            raise CommandError(f"level is not an integer: {raw!r}") from err
        return self.agent.set_brightness(level)

    def _reboot(self, _params: dict[str, str]) -> dict[str, Any]:
        """Reboot the host.

        SCHEDULED, NOT IMMEDIATE, and that is the whole design. An immediate
        reboot kills this process before the HTTP reply is written, so the
        caller sees a connection reset and cannot tell a reboot that happened
        from one that was refused. A one-second delay lets the 200 leave first.
        """
        # `sudo -n`, and the sudoers drop-in grants this one verb and no other.
        # Without -n a missing rule turns into a password prompt on a process
        # with no tty, which hangs until the timeout and reports as a transport
        # failure rather than as the permissions problem it is.
        # PROVE THE PRIVILEGE BEFORE PROMISING THE REBOOT. The scheduled shell
        # is detached and its failure reaches nobody, so without this probe a
        # refused sudo returns {"rebooting": true} and the panel simply does not
        # reboot — the exact silent-success shape this agent was built to
        # refuse. `sudo -n true` is the cheapest privileged no-op and its stderr
        # names the real cause, including the one that has actually bitten:
        # NoNewPrivileges=yes in the unit stops sudo elevating at all.
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["sudo", "-n", "true"], capture_output=True, text=True,
                timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise CommandError(f"cannot reach sudo: {err}",
                               HTTPStatus.BAD_GATEWAY) from err
        if probe.returncode != 0:
            raise CommandError(
                "cannot reboot: this agent may not use sudo. "
                + (probe.stderr or "").strip().replace("\n", " "),
                HTTPStatus.BAD_GATEWAY,
            )

        try:
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                ["/bin/sh", "-c", "sleep 1; sudo -n systemctl reboot"],
                start_new_session=True,
            )
        except OSError as err:
            raise CommandError(f"cannot schedule a reboot: {err}",
                               HTTPStatus.BAD_GATEWAY) from err
        return {"rebooting": True, "inSeconds": 1}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"pikioskd/{__version__}"
    sys_version = ""  # Do not advertise the Python version to the LAN.

    # Set by the server factory below.
    commands: CommandTable
    settings: Any

    # --- plumbing -----------------------------------------------------------

    def safe_path(self) -> str:
        """The request path with every secret-bearing parameter redacted.

        THE DEFAULT HANDLER WRITES THE FULL REQUEST LINE, AND THE REQUEST LINE
        CONTAINS THE PASSWORD — on every single poll, at whatever level the
        journal is keeping. Overriding ``log_message`` alone is NOT enough and
        that was the bug this method exists to fix: ``log_request`` builds its
        own line from ``self.requestline`` and hands it down, so the secret
        arrives already interpolated and a redaction applied to the format
        string never sees it. Redaction therefore happens on the value, here,
        and both log hooks route through it.
        """
        parts = urllib.parse.urlsplit(self.path)
        if not parts.query:
            return parts.path
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        redacted = [(k, "<redacted>" if k in _SECRET_PARAMS else v)
                    for k, v in pairs]
        return f"{parts.path}?{urllib.parse.urlencode(redacted)}"

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        _LOGGER.debug("%s %s %s -> %s", self.address_string(), self.command,
                      self.safe_path(), code)

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        # A belt-and-braces pass for anything that reaches this hook with the
        # request line already interpolated (send_error does). Asserted by
        # test_the_password_is_not_written_to_the_log.
        message = _SECRET_IN_TEXT.sub(r"\1<redacted>", message)
        _LOGGER.debug("%s - %s", self.address_string(), message)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _read_body_params(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        # Bounded: an unbounded read on a LAN-facing socket is a way to make
        # the agent hold a wall's worth of memory. No legitimate command body
        # is anywhere near this.
        if length > 64 * 1024:
            raise CommandError("request body too large",
                               HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type == "application/json":
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError as err:
                raise CommandError(f"malformed JSON body: {err}") from err
            if not isinstance(parsed, dict):
                raise CommandError("JSON body must be an object")
            return {str(k): "" if v is None else str(v) for k, v in parsed.items()}
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _authorised(self, params: dict[str, str]) -> bool:
        """Constant-time comparison against the configured password.

        AN EMPTY PASSWORD REFUSES EVERYTHING rather than allowing everything.
        A device that has not been given a secret is a device that has not been
        configured, and the fail-open reading of that is an open remote-admin
        API on a LAN — the installer generates one precisely so this case does
        not arise in practice, and it is refused here so that it cannot.
        """
        expected = str(self.settings.get("remoteAdminPassword"))
        if not expected:
            return False
        supplied = params.get("password", "")
        header = self.headers.get("Authorization") or ""
        if not supplied and header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        return hmac.compare_digest(supplied, expected)

    def _handle(self) -> None:
        try:
            parts = urllib.parse.urlsplit(self.path)
            params = {k: v[0] for k, v in
                      urllib.parse.parse_qs(parts.query,
                                            keep_blank_values=True).items()}
            params.update(self._read_body_params())

            if not self.settings.get("remoteAdmin"):
                raise CommandError("remote administration is disabled on this "
                                   "device", HTTPStatus.FORBIDDEN)
            if not self._authorised(params):
                # No hint about whether the password was absent or wrong, and
                # no WWW-Authenticate challenge — a browser prompting for
                # credentials on a wall is a dialog nobody can dismiss.
                raise CommandError("unauthorised", HTTPStatus.UNAUTHORIZED)

            command = params.get("cmd")
            if not command:
                raise CommandError(
                    "no cmd given. Known commands: "
                    + ", ".join(self.commands.names())
                )
            result = self.commands.dispatch(command, params)
        except CommandError as err:
            self._send_json({"status": "Error", "statustext": str(err)},
                            err.status)
            return
        except Exception as err:  # noqa: BLE001 - never leak a traceback to the LAN
            _LOGGER.exception("unhandled error serving %s", self.path)
            self._send_json(
                {"status": "Error", "statustext": f"internal error: {err}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if isinstance(result, tuple) and len(result) == 2 and \
                isinstance(result[0], (bytes, bytearray)):
            self._send_bytes(bytes(result[0]), str(result[1]))
            return
        payload: dict[str, Any] = {"status": "OK"}
        if isinstance(result, dict):
            payload.update(result)
        elif result is not None:
            payload["result"] = result
        self._send_json(payload, HTTPStatus.OK)

    def _send_json(self, payload: dict[str, Any], status: int) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class AgentServer(ThreadingHTTPServer):
    """Threaded so a slow screenshot cannot block a health poll."""

    daemon_threads = True
    # A wall restarts often during provisioning; without this the port sits in
    # TIME_WAIT and the unit fails to bind on the restart that was meant to fix
    # whatever was wrong.
    allow_reuse_address = True

    def __init__(self, agent: KioskAgent, address: tuple[str, int]) -> None:
        self.agent = agent
        commands = CommandTable(agent)
        settings = agent.settings

        class BoundHandler(_Handler):
            pass

        BoundHandler.commands = commands
        BoundHandler.settings = settings
        super().__init__(address, BoundHandler)

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="http",
                                 daemon=True)
        thread.start()
        return thread


def build_server(agent: KioskAgent, host: str = "0.0.0.0",
                 port: int | None = None) -> AgentServer:
    socketserver.TCPServer.allow_reuse_address = True
    resolved = int(agent.settings.get("remoteAdminPort")) if port is None else port
    return AgentServer(agent, (host, resolved))
