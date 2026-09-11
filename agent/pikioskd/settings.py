"""The agent's settings map — one JSON file, typed, atomically written.

MODELLED ON FULLY KIOSK'S SETTINGS SURFACE, WITH ONE DELIBERATE DEVIATION.
Fully serves and takes its whole settings map over ``listSettings`` /
``setStringSetting`` / ``setBooleanSetting``, and this agent answers the same
three commands with the same key names so an operator's muscle memory carries
over. What it does NOT reproduce is Fully's type infidelity: Fully returns
booleans as real JSON booleans but most integers as STRINGS, so
``timeToScreenOffV2`` reads ``'0'`` and a plain ``!= 0`` comparison calls every
correctly-configured device drifted, forever. That behaviour is a bug being
worked around estate-wide; reproducing it for compatibility would propagate the
workaround into a second fleet. Here a key declared ``int`` reads back an
``int``, always, and ``setStringSetting`` on an int key coerces or fails loudly
rather than storing a string that reads correct and compares wrong.

The second Fully trap this avoids: writing a bool through ``setStringSetting``
answers 200 and silently no-ops there, AND so does the mirror on some keys, so
a caller has to write, re-read and fall back to the other setter. Here every
setter routes to one typed write and every write is verified by the caller
reading the returned map — one path, one outcome.

The file is the ONLY persistent state the agent has. There is no second store,
no cache beside it and nothing derived from it kept on disk, so a settings file
plus the systemd unit fully describes a panel — which is what makes a wall
reprovisionable from this repository alone rather than from whatever was typed
into it once.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = "/etc/pikioskd/settings.json"


class SettingError(ValueError):
    """A setting was rejected. The message is returned to the caller verbatim."""


def _as_bool(value: Any) -> bool:
    """Accept what a URL query string can actually carry.

    A query parameter is always a string, so ``?value=true`` and ``?value=1``
    both have to mean True and ``?value=false`` must NOT — which is what a bare
    ``bool(value)`` would make it, since every non-empty string is truthy. That
    is the single most common way a remote-admin API grows a setting that can
    be turned on and never off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise SettingError(f"not a boolean: {value!r}")


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise SettingError(f"not an integer: {value!r}")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingError(f"not an integer: {value!r}") from None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise SettingError(f"not a string: {value!r}")
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    """A list, or a whitespace-separated string of the kind a query carries.

    SPLIT ON WHITESPACE, NEVER ON A COMMA. Chromium's own flags contain
    commas — ``--disable-features=WaylandFractionalScaleV1,Translate`` is one
    flag, not two — so a comma-delimited channel carries its own delimiter and
    tears that flag in half. Whitespace cannot collide, because a flag token
    produced by a whitespace split can never itself contain whitespace.
    """
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [token for token in str(value).split() if token]


def _clamp_byte(value: Any) -> int:
    number = _as_int(value)
    if not 0 <= number <= 255:
        raise SettingError(f"out of range 0-255: {number}")
    return number


def _non_negative(value: Any) -> int:
    number = _as_int(value)
    if number < 0:
        raise SettingError(f"must not be negative: {number}")
    return number


def _rotation(value: Any) -> str:
    text = _as_str(value).strip().lower() or "normal"
    allowed = ("normal", "90", "180", "270", "flipped", "flipped-90",
               "flipped-180", "flipped-270")
    if text not in allowed:
        raise SettingError(f"rotation must be one of {', '.join(allowed)}")
    return text


class _Spec:
    """One settable key: its type, its default, and its coercion."""

    __slots__ = ("kind", "default", "coerce", "secret")

    def __init__(self, kind: str, default: Any,
                 coerce: Callable[[Any], Any], secret: bool = False) -> None:
        self.kind = kind
        self.default = default
        self.coerce = coerce
        self.secret = secret


# The declared surface. A key absent from here cannot be written, which is what
# stops a typo becoming a silently-persisted setting nothing ever reads.
SPECS: dict[str, _Spec] = {
    # --- identity and access ------------------------------------------------
    "deviceName": _Spec("string", "", _as_str),
    "remoteAdmin": _Spec("bool", True, _as_bool),
    "remoteAdminPassword": _Spec("string", "", _as_str, secret=True),
    "remoteAdminPort": _Spec("int", 2323, _non_negative),
    # --- what the wall shows ------------------------------------------------
    "startURL": _Spec("string", "about:blank", _as_str),
    # A page of your choosing on ANY failed load. This is not decoration: it is
    # a per-device outage notice that needs no automation, survives a reboot,
    # and is the only thing a wall can say for itself when the board server it
    # points at stops answering. Empty disables the substitution.
    "errorURL": _Spec("string", "", _as_str),
    "kioskMode": _Spec("bool", True, _as_bool),
    "maintenanceMode": _Spec("bool", False, _as_bool),
    # --- screen -------------------------------------------------------------
    # 0 means never, on both timers, matching Fully's own semantics.
    "timeToScreenOffV2": _Spec("int", 0, _non_negative),
    "timeToScreensaverV2": _Spec("int", 0, _non_negative),
    "screenBrightness": _Spec("int", 255, _clamp_byte),
    "screensaverBrightness": _Spec("int", 0, _clamp_byte),
    "screensaverWallpaperURL": _Spec("string", "", _as_str),
    "outputName": _Spec("string", "", _as_str),
    "rotation": _Spec("string", "normal", _rotation),
    # --- browser ------------------------------------------------------------
    "browserRestartOnCrash": _Spec("bool", True, _as_bool),
    "browserRestartBackoffSeconds": _Spec("int", 5, _non_negative),
    "cdpPort": _Spec("int", 9222, _non_negative),
    "chromiumBinary": _Spec("string", "chromium", _as_str),
    "cageBinary": _Spec("string", "cage", _as_str),
    "chromiumFlags": _Spec("list", [], _as_str_list),
    # These panels have no pointer device, and Chromium's Ozone/Wayland
    # cursor path strands whatever cursor was drawn at launch. On by
    # default because a wall board has no use for a pointer it cannot
    # move; off exists for a bench host somebody is actually driving
    # (jrackerby/kiosk-pi#9).
    "hideCursor": _Spec("bool", True, _as_bool),
    # NOT under /home: the unit sets ProtectHome=read-only, and Chromium exits 21
    # on every launch when it cannot write its own profile. /var/lib/pikioskd is
    # the unit's StateDirectory, created and owned by systemd for this service.
    "chromiumProfileDir": _Spec("string", "/var/lib/pikioskd/chromium",
                                _as_str),
    "chromiumCacheDir": _Spec("string", "/tmp/chromium-cache", _as_str),
    # DEFERRED TO, NEVER OVERWRITTEN, when the environment already carries one.
    # The fleet converged on this single value and a per-host variant is a
    # defect rather than hardware; see the installer, which writes the same.
    "xdgRuntimeDir": _Spec("string", "/run/kiosk-wayland", _as_str),
}

BOOLEAN_KEYS = frozenset(k for k, s in SPECS.items() if s.kind == "bool")
INT_KEYS = frozenset(k for k, s in SPECS.items() if s.kind == "int")
SECRET_KEYS = frozenset(k for k, s in SPECS.items() if s.secret)

# Changing one of these has no effect until the browser is restarted, so the
# setter that touches one says so in its reply rather than letting the caller
# conclude from a 200 that the wall has already moved.
BROWSER_RESTART_KEYS = frozenset({
    "cdpPort", "chromiumBinary", "cageBinary", "chromiumFlags", "hideCursor",
    "chromiumProfileDir", "chromiumCacheDir", "xdgRuntimeDir", "kioskMode",
})


def defaults() -> dict[str, Any]:
    return {key: spec.default for key, spec in SPECS.items()}


class Settings:
    """The live settings map, backed by one JSON file.

    Every mutation writes the whole file through a temp file and ``os.replace``,
    so a power cut during a write leaves either the old map or the new one and
    never a half-written one. A wall recovering from a truncated settings file
    would come up on ``about:blank`` with no password set, which is the one
    failure this class exists to make impossible.
    """

    def __init__(self, path: str = DEFAULT_SETTINGS_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._values = defaults()
        self._listeners: list[Callable[[set[str]], None]] = []

    @property
    def path(self) -> str:
        return self._path

    def add_listener(self, callback: Callable[[set[str]], None]) -> None:
        """Called with the set of changed keys after every successful write."""
        self._listeners.append(callback)

    def load(self) -> None:
        """Read the file into memory. A missing file is a fresh install.

        AN UNPARSEABLE FILE IS NOT TREATED AS AN EMPTY ONE. Falling back to
        defaults there would silently drop the admin password and reopen the
        API to anybody, so the exception propagates and the unit fails to
        start — a dark wall that says why beats a live wall that is open.
        """
        with self._lock:
            if not os.path.exists(self._path):
                _LOGGER.info("no settings file at %s; using defaults", self._path)
                return
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise SettingError(f"{self._path} does not contain a JSON object")
            merged = defaults()
            for key, value in raw.items():
                spec = SPECS.get(key)
                if spec is None:
                    _LOGGER.warning("ignoring unknown setting %r in %s", key,
                                    self._path)
                    continue
                try:
                    merged[key] = spec.coerce(value)
                except SettingError as err:
                    # One bad value loses one key, never the file. The default
                    # is announced rather than assumed, because a setting that
                    # quietly reverted is the hardest kind to diagnose from a
                    # dashboard that looks merely wrong.
                    _LOGGER.error("setting %s rejected (%s); using default %r",
                                  key, err, spec.default)
            self._values = merged

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False,
                prefix=".settings-", suffix=".json",
            )
            try:
                with handle:
                    json.dump(self._values, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                # The file carries the remote-admin password, so it is never
                # world-readable even for the instant between create and
                # chmod — NamedTemporaryFile already creates at 0600 and this
                # only re-asserts it after the rename.
                os.chmod(handle.name, 0o600)
                os.replace(handle.name, self._path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(handle.name)
                raise

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in SPECS:
                raise SettingError(f"unknown setting: {key}")
            return self._values[key]

    def as_dict(self, redact: bool = False) -> dict[str, Any]:
        """The whole map. ``redact`` blanks the secrets for logs and diagnostics.

        The HTTP surface does NOT redact: the caller already proved it holds the
        password, so hiding it from that caller protects nothing and breaks the
        one legitimate use — an operator reading back what a device is
        configured with. Redaction is for anything that leaves that channel.
        """
        with self._lock:
            values = dict(self._values)
        if redact:
            for key in SECRET_KEYS:
                if values.get(key):
                    values[key] = "**redacted**"
        return values

    def set_many(self, updates: dict[str, Any]) -> set[str]:
        """Coerce, validate, write, persist — or change nothing at all.

        ALL-OR-NOTHING ON PURPOSE. A partial application would leave the device
        in a state no caller asked for and no caller can name, and the reply
        would have to describe it. Every value is coerced before the first one
        is stored, so a rejected key aborts with the map untouched.
        """
        coerced: dict[str, Any] = {}
        for key, value in updates.items():
            spec = SPECS.get(key)
            if spec is None:
                raise SettingError(f"unknown setting: {key}")
            coerced[key] = spec.coerce(value)
        with self._lock:
            changed = {key for key, value in coerced.items()
                       if self._values.get(key) != value}
            if not changed:
                return set()

            # ROLL MEMORY BACK IF THE FILE WRITE FAILS, or all-or-nothing is a
            # claim this method does not honour. Updating memory and then
            # failing to persist leaves the agent running on a value that is
            # not in the settings file: `listSettings` reads it back as
            # correct, the panel behaves as if it took, and the change is gone
            # at the next restart with nothing on the request path to say so.
            #
            # NOT HYPOTHETICAL. It shipped as an unwritable /etc/pikioskd —
            # fixed in install.sh — but the permissions were only the trigger.
            # A full SD card does it, and so does ext4 flipping to read-only as
            # the card wears out, which this fleet has a binary_sensor for
            # precisely because it happens. Measured against an unwritable
            # directory as an unprivileged user, 2026-09-10: save() raises and
            # memory was left holding the new value.
            previous = {key: self._values.get(key) for key in coerced}
            self._values.update(coerced)
            try:
                self.save()
            except OSError:
                self._values.update(previous)
                raise
        for listener in self._listeners:
            try:
                listener(set(changed))
            except Exception:  # noqa: BLE001 - a listener must never break a write
                _LOGGER.exception("settings listener failed for %s", changed)
        return changed

    def set(self, key: str, value: Any) -> set[str]:
        return self.set_many({key: value})
