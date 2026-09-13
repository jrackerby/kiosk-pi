"""``sd_notify(3)`` over the notify socket, with no libsystemd and no dependency.

WHY THE AGENT TALKS TO SYSTEMD AT ALL. Under ``Type=simple`` with
``Restart=always``, systemd knows exactly one thing about this process: whether
it has exited. A supervision loop deadlocked on a lock, an HTTP thread wedged
behind a socket that never times out, a tick that hung inside a subprocess
call — every one of those reads ``active (running)`` for ever, and the wall
behind it is whatever Chromium last painted. The agent's own pitch is that the
restart COUNT is the signal and ``is-active`` proves nothing; that argument
applies to the agent exactly as much as to the browser it supervises.

So the unit is ``Type=notify`` with a ``WatchdogSec``, and the agent proves it
is alive by saying so — ``READY=1`` once the API is bound, ``WATCHDOG=1`` on a
cadence, from the one place that can vouch for both threads. Miss the interval
and systemd kills and restarts the agent, which is the recovery a hung
supervisor needs and cannot give itself.

STANDARD LIBRARY ONLY. The protocol is a datagram on a Unix socket named by
``$NOTIFY_SOCKET``; a leading ``@`` marks the abstract namespace. ``sdnotify``
on PyPI is forty lines that do the same thing, and a dependency here would
turn provisioning from a file copy into a package install (see install.sh).

WITHOUT A SOCKET EVERY CALL IS A NO-OP THAT SAYS SO. Run by hand or under a
test there is no systemd listening; the functions return False and the agent
carries on, because notification is a report to a supervisor, not a
precondition for serving.
"""

from __future__ import annotations

import logging
import os
import socket

_LOGGER = logging.getLogger(__name__)


def socket_path() -> str | None:
    """Where systemd is listening, or None when nothing is."""
    return os.environ.get("NOTIFY_SOCKET") or None


def notify(state: str, environ: dict[str, str] | None = None) -> bool:
    """Send one state string. Returns whether anything was listening.

    A failure to send is logged at DEBUG and swallowed: the worst outcome of a
    lost datagram is that the watchdog fires and restarts a process that was in
    fact healthy, and the worst outcome of raising here would be to kill it
    ourselves, sooner, for the same reason.
    """
    env = os.environ if environ is None else environ
    address = env.get("NOTIFY_SOCKET") or ""
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
    except OSError as err:
        _LOGGER.debug("sd_notify(%r) not delivered: %s", state, err)
        return False
    return True


def ready() -> bool:
    return notify("READY=1")


def stopping() -> bool:
    return notify("STOPPING=1")


def watchdog_ping() -> bool:
    return notify("WATCHDOG=1")


def watchdog_interval(environ: dict[str, str] | None = None) -> float | None:
    """The interval systemd expects a ping within, in seconds, or None.

    Read from ``WATCHDOG_USEC``, and ONLY when ``WATCHDOG_PID`` names this
    process or is absent — systemd sets the pid so a child that inherited the
    environment does not think the watchdog is its own to feed.
    """
    env = os.environ if environ is None else environ
    raw = env.get("WATCHDOG_USEC")
    if not raw:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid and pid != str(os.getpid()):
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    return usec / 1_000_000 if usec > 0 else None
