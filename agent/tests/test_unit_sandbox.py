"""The systemd unit's sandbox against the paths the agent actually writes.

THIS FILE EXISTS BECAUSE THE SHIPPED 1.0.0 UNIT WAS UNRUNNABLE AND EVERY OTHER
TEST PASSED. ``ProtectHome=read-only`` made Chromium's ``--user-data-dir``
unwritable, so the browser exited 21 within a second of every launch while the
unit itself reported ``active`` and ``NRestarts=0`` — the failure mode this
repository's TOOLS.md already records, arriving from the one direction
nobody had a check pointed at. Measured on the first panel, 2026-09-10.

The check is a join between two files that must agree and had no reason to:
the unit's writable set and the settings default for the profile directory.
"""

from __future__ import annotations

import configparser
import pathlib

import pytest

from pikioskd.settings import defaults

UNIT_PATH = pathlib.Path(__file__).resolve().parents[1] / "systemd" / "pikioskd.service"


def _unit():
    # systemd unit files are ini-shaped but repeat keys; strict=False keeps
    # configparser from raising on that rather than silently taking one.
    parser = configparser.ConfigParser(strict=False, allow_no_value=True)
    parser.optionxform = str
    parser.read_text = None  # noqa: F841 - guard against accidental use
    parser.read_string(UNIT_PATH.read_text(encoding="utf-8"))
    return parser


def writable_roots(parser):
    """Every path the unit leaves writable for the service."""
    roots = set()
    service = parser["Service"]
    for raw in service.get("ReadWritePaths", "").split():
        if raw:
            roots.add(raw)
    for raw in service.get("StateDirectory", "").split():
        if raw:
            roots.add(f"/var/lib/{raw}")
    if service.get("ProtectHome", "no").strip() not in {"yes", "read-only", "tmpfs"}:
        roots.add("/home")
    return roots


def _is_under(path: str, roots) -> bool:
    candidate = pathlib.PurePosixPath(path)
    return any(candidate == pathlib.PurePosixPath(r) or
               pathlib.PurePosixPath(r) in candidate.parents for r in roots)


@pytest.mark.parametrize("key", ["chromiumProfileDir", "chromiumCacheDir"])
def test_the_browser_directories_are_writable_under_the_sandbox(key):
    """Chromium must be able to write both, or it exits 21 on every launch."""
    parser = _unit()
    roots = writable_roots(parser)
    path = defaults()[key]
    assert _is_under(path, roots), (
        f"{key} defaults to {path!r}, which no directive in pikioskd.service "
        f"leaves writable (writable roots: {sorted(roots)}). Chromium exits 21 "
        f"when it cannot write this directory, and the unit still reads active."
    )


def test_protect_home_is_actually_on():
    """The check above is only meaningful while the sandbox is real."""
    assert _unit()["Service"].get("ProtectHome", "no").strip() == "read-only"


def test_start_limit_interval_is_not_in_the_service_section():
    """systemd parses it in [Service] as an unknown key and IGNORES it."""
    parser = _unit()
    assert "StartLimitIntervalSec" not in parser["Service"], (
        "StartLimitIntervalSec in [Service] is ignored by systemd — it belongs "
        "in [Unit], and the journal says so once in a line nobody reads."
    )
    assert "StartLimitIntervalSec" in parser["Unit"]


def test_the_join_can_fail():
    """Self-test: the assertion catches the exact regression that shipped.

    Without this, a check that never fails reads identically to one that
    passes for a good reason.
    """
    parser = _unit()
    roots = writable_roots(parser)
    assert not _is_under("/home/kiosk/.config/chromium-kiosk", roots), (
        "the 1.0.0 default must NOT be judged writable by this check"
    )


# --- the privilege the unit must not forbid ---------------------------------

def _agent_shells_to_sudo() -> bool:
    """Does any agent module shell out to sudo.

    Read from the SOURCE rather than from a list kept here: a hardcoded "yes"
    would keep asserting after the dependency was removed, and a hardcoded "no"
    would stop asserting the moment somebody added one back.
    """
    package = pathlib.Path(__file__).resolve().parents[1] / "pikioskd"
    for module in package.glob("*.py"):
        for line in module.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if '"sudo"' in code or "'sudo'" in code or "sudo -n" in code:
                return True
    return False


def test_no_new_privileges_is_off_while_the_agent_needs_sudo():
    """NoNewPrivileges=yes FORBIDS THE ONE PRIVILEGED VERB THIS AGENT HAS.

    PR_SET_NO_NEW_PRIVS stops a setuid binary elevating at all, and sudo
    refuses outright rather than falling back — "The \"no new privileges\"
    flag is set, which prevents sudo from running as root." Measured against a
    real setuid sudo, 2026-09-10.

    It fails SILENTLY in production: the reboot is scheduled in a detached
    shell whose stderr reaches nobody, so the panel answers 200 and never
    reboots. Same shape as ProtectHome killing the browser while the unit read
    healthy — a sandbox directive forbidding the thing the service exists to do.
    """
    if not _agent_shells_to_sudo():
        pytest.skip("the agent no longer uses sudo; this pairing cannot bite")
    value = _unit()["Service"].get("NoNewPrivileges", "no").strip()
    assert value == "no", (
        "NoNewPrivileges=" + value + " in pikioskd.service, but the agent "
        "shells to sudo for rebootDevice. sudo cannot elevate under "
        "PR_SET_NO_NEW_PRIVS, so the reboot button would answer OK and do "
        "nothing. The privilege boundary is /etc/sudoers.d/pikioskd, which "
        "grants exactly one command."
    )


def test_the_sudo_detection_can_fail(tmp_path):
    """Self-test: the source scan must actually find a sudo call.

    Without this, `_agent_shells_to_sudo` returning False for a silly reason
    would skip the test above for ever, and a skipped guard reads exactly like
    a passing one.
    """
    assert _agent_shells_to_sudo(), (
        "the scan found no sudo call in pikioskd/, but server.py's rebootDevice "
        "uses one — the detection has broken, not the dependency"
    )


def test_the_reboot_verb_is_still_the_only_privileged_one():
    """The sudoers drop-in is the real boundary, so it stays narrow.

    NoNewPrivileges is off, which means anything the drop-in grants is
    genuinely reachable. `ALL` here would hand a LAN-facing HTTP service on a
    shared password full root.
    """
    drop_in = (pathlib.Path(__file__).resolve().parents[1]
               / "systemd" / "pikioskd-sudo").read_text(encoding="utf-8")
    granted = [line for line in drop_in.splitlines()
               if line.strip() and not line.strip().startswith("#")]
    assert len(granted) == 1, f"expected one grant line, got {granted}"
    assert "NOPASSWD:" in granted[0]
    assert "ALL" not in granted[0].split("NOPASSWD:", 1)[1], (
        "the sudoers drop-in grants ALL; with NoNewPrivileges off that is real "
        "root for anything that can reach the admin API"
    )
    assert "systemctl reboot" in granted[0]


# --- the installer's directory modes vs what the agent writes ----------------

INSTALL_SH = pathlib.Path(__file__).resolve().parents[1] / "install.sh"


def _install_directive(target_var: str):
    """The `install -d` line that creates the directory held in $<var>."""
    for line in INSTALL_SH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("install -d") and f'"${target_var}"' in stripped:
            return stripped
    return None


def test_the_config_directory_is_writable_by_the_user_the_unit_runs_as():
    """THE AGENT WRITES ITS OWN SETTINGS FILE, so it must own the directory.

    Settings are persisted by an atomic replace — temp file beside the target,
    fsync, rename — which needs write permission on the DIRECTORY. Created
    root-owned at 0750 the directory was readable and unwritable, so every
    setting the HTTP API accepted changed in memory, answered 200, and was
    lost on the next restart. The traceback went to the journal, where a
    working read-back made it look like nothing was wrong. Measured on
    the first panel, 2026-09-10, on the first setting ever written from HA.
    """
    line = _install_directive("CONFIG_DIR")
    assert line is not None, "install.sh no longer creates CONFIG_DIR"

    unit_user = _unit()["Service"]["User"].strip()
    # Either owned by that user, or group-owned by it AND group-writable.
    owned = f'-o "$KIOSK_USER"' in line or f"-o {unit_user}" in line
    mode = ""
    parts = line.split()
    if "-m" in parts:
        mode = parts[parts.index("-m") + 1]
    group_writable = bool(mode) and int(mode[-2]) & 0o2

    assert owned or group_writable, (
        f"{line!r} creates the settings directory unwritable by the unit's "
        f"User={unit_user}; the agent cannot persist a single setting."
    )


def test_the_install_directive_check_can_fail():
    """Self-test: the parse finds a real line, so a pass means something."""
    assert _install_directive("CONFIG_DIR") is not None
    assert _install_directive("NO_SUCH_VARIABLE") is None
