"""The screen: on, off, brightness, geometry — and what is honestly unknown.

THREE INSTRUMENTS, IN PREFERENCE ORDER, AND THE ORDER IS MEASURED NOT GUESSED.

1. ``wlr-randr`` against the compositor. This is the primary and it is known to
   work on this fleet: the panels run Chromium under ``cage``, which implements
   wlr-output-management, and the existing fleet collector already reads output
   name, model, enabled state and mode back from it on every host. It is the
   only instrument that reports geometry, and the only one whose "off" the
   compositor itself agrees with.
2. ``vcgencmd display_power``. Raspberry Pi firmware, on/off only, no geometry.
   Used when the compositor is not up — which is precisely the window in which
   a wall most needs to be switchable, since the browser has not started yet.
3. ``/sys/class/backlight/*``. The ONLY brightness instrument, and it exists
   only for a DSI or DPI panel. AN HDMI MONITOR HAS NO BACKLIGHT NODE, so on a
   fleet driving desktop monitors brightness is genuinely unavailable, and this
   module says so by returning None rather than by returning a number nothing
   controls. A brightness entity that moves a slider and changes nothing is
   worse than an absent one: it teaches an operator that the control works.

WHAT "SCREEN OFF" MEANS HERE IS DPMS, NOT A BLACK PAGE. Disabling the output
puts the monitor into standby and the Pi stops scanning out, which is the
saving a wall is turned off for. Painting a black page instead leaves the
backlight lit and the GPU composing, and reads identically from the API — the
distinction is invisible to anything but a power meter, which is why it has to
be decided here and stated rather than left to whichever call happens to be
convenient.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
from typing import Any

_LOGGER = logging.getLogger(__name__)

# A display call that hangs is a wall that cannot be recovered without SSH, so
# every instrument is bounded. These are deliberately short: all three read
# local hardware and none legitimately takes a second.
_CALL_TIMEOUT = 8

_MODE_RE = re.compile(r"^\s*(\d+)x(\d+)\s*px")


class DisplayError(RuntimeError):
    """The screen could not be read or driven by any available instrument."""


def _run(argv: list[str], env: dict[str, str] | None = None,
         timeout: int = _CALL_TIMEOUT) -> tuple[int, str, str]:
    """Run a command, never raise on a non-zero exit, always come back.

    A TIMEOUT IS A FINDING, not a retry. It is reported as exit code 124 (the
    conventional one) with the reason on stderr, so a caller can tell a screen
    that answered "no" from a screen that did not answer.
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout,
            env=merged, check=False,
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout}s"
    return completed.returncode, completed.stdout, completed.stderr


class Display:
    """One physical output, addressed through whichever instrument answers."""

    def __init__(self, output_name: str = "", xdg_runtime_dir: str = "",
                 wayland_display: str = "wayland-0") -> None:
        self._configured_output = output_name
        self._xdg_runtime_dir = xdg_runtime_dir
        self._wayland_display = wayland_display

    # --- instrument plumbing ------------------------------------------------

    def _wayland_env(self) -> dict[str, str]:
        """The compositor's own environment.

        XDG_RUNTIME_DIR IS DEFERRED TO, NEVER OVERWRITTEN. If the process
        already carries one — which it does when systemd started the agent as a
        user service, or when the operator is running it by hand — that value
        is the correct one and the configured default is a fallback. Writing
        over it is how a fleet grows a per-host variant that looks like
        hardware and is not.
        """
        env = {"WAYLAND_DISPLAY": self._wayland_display}
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or self._xdg_runtime_dir
        if runtime_dir:
            env["XDG_RUNTIME_DIR"] = runtime_dir
        return env

    def _wlr_randr(self, *args: str) -> tuple[int, str, str]:
        return _run(["wlr-randr", *args], env=self._wayland_env())

    def parse_outputs(self, text: str) -> list[dict[str, Any]]:
        """Parse ``wlr-randr``'s human output into records.

        Its format is a header line per output and indented properties under
        it. Parsed line-oriented rather than by one big regex so a property
        this agent does not know about cannot break the ones it does.
        """
        outputs: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            if not line.startswith((" ", "\t")):
                current = {"name": line.split()[0], "modes": []}
                outputs.append(current)
                continue
            if current is None:
                continue
            stripped = line.strip()
            if stripped.startswith("Model:"):
                current["model"] = stripped[len("Model:"):].strip()
            elif stripped.startswith("Make:"):
                current["make"] = stripped[len("Make:"):].strip()
            elif stripped.startswith("Enabled:"):
                current["enabled"] = stripped[len("Enabled:"):].strip().lower() == "yes"
            elif stripped.startswith("Transform:"):
                current["transform"] = stripped[len("Transform:"):].strip()
            elif stripped.startswith("Scale:"):
                current["scale"] = stripped[len("Scale:"):].strip()
            elif "px," in stripped:
                match = _MODE_RE.match(line)
                if match:
                    mode = {
                        "width": int(match.group(1)),
                        "height": int(match.group(2)),
                        "current": "current" in stripped,
                        "preferred": "preferred" in stripped,
                    }
                    current["modes"].append(mode)
                    if mode["current"]:
                        current["mode"] = f"{mode['width']}x{mode['height']}"
        return outputs

    def outputs(self) -> list[dict[str, Any]]:
        code, out, err = self._wlr_randr()
        if code != 0:
            _LOGGER.debug("wlr-randr unavailable (rc=%s): %s", code, err.strip())
            return []
        return self.parse_outputs(out)

    def output_name(self) -> str | None:
        """Which output to drive.

        ASK THE COMPOSITOR, NEVER THE TABLE. A configured name wins so a
        multi-output host can be pinned, but the default is to read the first
        output the compositor reports rather than to hardcode ``HDMI-A-1``,
        which is correct on a Pi 4 driving its first port and wrong on
        everything else — including the same Pi with the cable in the other
        socket.
        """
        if self._configured_output:
            return self._configured_output
        outputs = self.outputs()
        return outputs[0]["name"] if outputs else None

    # --- state --------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Everything readable about the screen, with unknowns left as None.

        ``on`` IS TRI-STATE ON PURPOSE: True, False, or None for "no instrument
        could say". ``ok at zero`` and ``could not read`` are different values
        at the source, and a monitor that reports False when it simply failed
        to look is the fail-permissive shape that makes a dark wall read
        healthy.
        """
        record: dict[str, Any] = {
            "on": None,
            "output": None,
            "make": None,
            "model": None,
            "resolution": None,
            "orientation": None,
            "brightness": None,
            "brightnessMax": None,
            "instrument": None,
        }
        outputs = self.outputs()
        if outputs:
            wanted = self._configured_output
            chosen = next(
                (o for o in outputs if o["name"] == wanted), outputs[0]
            ) if wanted else outputs[0]
            # MAKE AND MODEL ARE REPORTED SEPARATELY, NOT COLLAPSED. Both are
            # EDID fields and `parse_outputs` has read both since 1.0.0; the
            # record used to publish `model or make`, so a monitor that reports
            # only a make was indistinguishable from one that reports only a
            # model, and the make was unrecoverable from the payload either way
            # (jrackerby/kiosk-pi#7). Either half may legitimately be absent —
            # an EDID-less output reports neither — so both stay None-able and
            # the consumer decides how to render one half.
            record.update({
                "on": chosen.get("enabled"),
                "output": chosen.get("name"),
                "make": chosen.get("make"),
                "model": chosen.get("model"),
                "resolution": chosen.get("mode"),
                "orientation": chosen.get("transform"),
                "instrument": "wlr-randr",
            })
        else:
            power = self._vcgencmd_state()
            if power is not None:
                record["on"] = power
                record["instrument"] = "vcgencmd"

        brightness, maximum = self.brightness()
        record["brightness"] = brightness
        record["brightnessMax"] = maximum
        return record

    def _vcgencmd_state(self) -> bool | None:
        if shutil.which("vcgencmd") is None:
            return None
        code, out, _err = _run(["vcgencmd", "display_power"])
        if code != 0:
            return None
        # Answers `display_power=1`. Anything else is a firmware this parser
        # does not know, and guessing is worse than reporting unknown.
        text = out.strip()
        if text.endswith("=1"):
            return True
        if text.endswith("=0"):
            return False
        return None

    # --- power --------------------------------------------------------------

    def set_power(self, on: bool) -> str:
        """Turn the screen on or off. Returns the instrument that did it.

        BOTH INSTRUMENTS ARE TRIED, in order, and a failure of the first is not
        fatal. The compositor is the right answer when it is up; the firmware
        call is the one that still works when it is not, which is exactly the
        window — boot, a crashed browser, maintenance — in which somebody is
        most likely to be asking.
        """
        errors: list[str] = []
        name = self.output_name()
        if name:
            code, _out, err = self._wlr_randr(
                "--output", name, "--on" if on else "--off"
            )
            if code == 0:
                return "wlr-randr"
            errors.append(f"wlr-randr: {err.strip() or code}")

        if shutil.which("vcgencmd") is not None:
            code, _out, err = _run(["vcgencmd", "display_power", "1" if on else "0"])
            if code == 0:
                return "vcgencmd"
            errors.append(f"vcgencmd: {err.strip() or code}")

        raise DisplayError("; ".join(errors) or "no display instrument available")

    def set_rotation(self, rotation: str) -> str:
        name = self.output_name()
        if not name:
            raise DisplayError("no output to rotate: wlr-randr reported none")
        code, _out, err = self._wlr_randr("--output", name, "--transform", rotation)
        if code != 0:
            raise DisplayError(f"wlr-randr: {err.strip() or code}")
        return name

    # --- brightness ---------------------------------------------------------

    @staticmethod
    def _backlight_dir() -> str | None:
        """The first backlight device, or None where there is no such hardware.

        Sorted so the choice is stable across boots. Unsorted globs come back
        in directory order, which is arbitrary, so a two-backlight host would
        silently swap which one the slider drives.
        """
        candidates = sorted(glob.glob("/sys/class/backlight/*"))
        return candidates[0] if candidates else None

    def brightness(self) -> tuple[int | None, int | None]:
        """``(current, max)`` in the panel's own units, or ``(None, None)``.

        Not normalised to 0-255 here. The caller scales, because the raw
        maximum is itself a fact worth reporting: a panel with a max of 7 and
        one with a max of 255 behave very differently under a slider, and a
        pre-scaled value hides which one is on the wall.
        """
        directory = self._backlight_dir()
        if directory is None:
            return None, None
        try:
            with open(os.path.join(directory, "brightness"), encoding="ascii") as fh:
                current = int(fh.read().strip())
            with open(os.path.join(directory, "max_brightness"), encoding="ascii") as fh:
                maximum = int(fh.read().strip())
        except (OSError, ValueError) as err:
            _LOGGER.debug("backlight unreadable at %s: %s", directory, err)
            return None, None
        return current, maximum

    def set_brightness(self, level: int) -> int:
        """Set brightness from a 0-255 scale. Returns the raw value written.

        RAISES WHERE THERE IS NO BACKLIGHT, rather than succeeding quietly. The
        HTTP surface turns that into an error the integration turns into an
        unavailable entity — which is the honest end state for a wall driving
        an HDMI monitor, and it is reached by refusing rather than by pretending.
        """
        directory = self._backlight_dir()
        if directory is None:
            raise DisplayError(
                "no /sys/class/backlight device: this display has no "
                "software-controllable brightness"
            )
        _current, maximum = self.brightness()
        if maximum is None:
            raise DisplayError(f"{directory} has no readable max_brightness")
        raw = round(max(0, min(255, level)) * maximum / 255)
        try:
            with open(os.path.join(directory, "brightness"), "w",
                      encoding="ascii") as fh:
                fh.write(str(raw))
        except OSError as err:
            raise DisplayError(
                f"cannot write {directory}/brightness: {err}. The agent's user "
                "needs write access to the backlight device (see the udev rule "
                "the installer lays down)."
            ) from err
        return raw
