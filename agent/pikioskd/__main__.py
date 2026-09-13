"""``python3 -m pikioskd`` — the process systemd starts.

Deliberately thin. Everything it does is argument parsing, logging setup and
signal handling; the wall itself is ``KioskAgent``, which is constructible in a
test with no sockets, no browser and no screen.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import Any

from . import sdnotify
from .agent import KioskAgent
from .server import build_server
from .settings import DEFAULT_SETTINGS_PATH, Settings
from .version import __version__

_LOGGER = logging.getLogger("pikioskd")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pikioskd",
        description="A Fully-Kiosk-shaped remote-admin agent for a Raspberry "
                    "Pi wall panel.",
    )
    parser.add_argument("--settings", default=DEFAULT_SETTINGS_PATH,
                        help=f"settings file (default: {DEFAULT_SETTINGS_PATH})")
    parser.add_argument("--host", default="0.0.0.0",
                        help="address to bind the admin API to (default: all)")
    parser.add_argument("--port", type=int, default=None,
                        help="override the remoteAdminPort setting")
    parser.add_argument("--no-browser", action="store_true",
                        help="serve the API without launching a browser. For "
                             "bringing an agent up on a host whose display is "
                             "not wired yet, and for diagnosing one whose "
                             "browser will not start.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        # No timestamp: systemd's journal already stamps every line, and a
        # second one in the message is noise in every `journalctl` read.
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = Settings(args.settings)
    settings.load()

    if not settings.get("remoteAdminPassword"):
        # NOT FATAL, BUT LOUD. The API refuses every request without a
        # password, so an unconfigured device is closed rather than open — but
        # it is also useless, and a wall that is silently useless is worse than
        # one that says why in its own journal on every start.
        _LOGGER.error(
            "no remoteAdminPassword is set in %s: the admin API will refuse "
            "every request. Set one and restart.", args.settings
        )

    agent = KioskAgent(settings)
    server = build_server(agent, host=args.host, port=args.port)
    _LOGGER.info("pikioskd %s serving on %s:%d", __version__,
                 *server.server_address[:2])

    if args.no_browser:
        _LOGGER.warning("--no-browser: the wall will not be started")
    else:
        agent.start()

    stopping = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        # Idempotent: systemd sends SIGTERM and then SIGKILL, and a second
        # SIGTERM arriving during shutdown must not start a second shutdown.
        if stopping.is_set():
            return
        stopping.set()
        _LOGGER.info("signal %s: shutting down", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    http_thread = server.serve_in_thread()
    # READY ONLY ONCE THE API IS ACTUALLY SERVING. Under Type=notify systemd
    # holds `systemctl start` (and the installer behind it) until this arrives,
    # so a start that returns is a start whose loopback probe will answer.
    sdnotify.ready()
    interval = sdnotify.watchdog_interval()
    if interval:
        _LOGGER.info("systemd watchdog armed: fed once a second while both "
                     "threads are healthy, killed after %.0fs of silence",
                     interval)
    try:
        while not stopping.wait(1.0):
            if interval and _healthy(agent, http_thread, args.no_browser):
                sdnotify.watchdog_ping()
    finally:
        sdnotify.stopping()
        server.shutdown()
        server.server_close()
        agent.shutdown()
    return 0


def _healthy(agent: KioskAgent, http_thread: threading.Thread,
             no_browser: bool) -> bool:
    """Should systemd's watchdog be fed this second.

    THE PING IS A CLAIM AND IT IS ONLY MADE WHEN BOTH THREADS CAN BACK IT.
    Feeding the watchdog unconditionally from this loop would certify a
    process whose HTTP thread has died and whose supervision loop is wedged
    in a subprocess call — the exact two states a watchdog is for. Without a
    browser there is no supervision loop to vouch for, so only the API counts.
    """
    if not http_thread.is_alive():
        return False
    if no_browser:
        return True
    return agent.loop_healthy()


if __name__ == "__main__":
    raise SystemExit(main())
