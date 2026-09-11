"""Constants for the Kiosk Pi integration.

1.0.0 IS A REWRITE, NOT A RELEASE. Everything below 1.0.0 drove a Raspberry Pi
wall panel over SSH: it ran shell over a key, parsed ``KEY=value`` blocks out of
the result, and edited ``/home/kiosk/kiosk.sh`` in place to change what a wall
showed. This version drives ``pikioskd`` — an agent that runs ON the panel and
owns the browser — over its HTTP admin API, the way ``fully_kiosk`` drives a
Fully Kiosk tablet. The transport, the credential and the failure modes are all
different, so an existing entry cannot be migrated: see ``async_migrate_entry``
in ``__init__.py``, which says so to the operator rather than silently producing
an entry that cannot connect.

WHAT LEFT, AND WHERE IT WENT. Generic OS health — apt state, kernel upgrades,
dmesg, NIC inventory, config drift — belongs to a general host monitor such as
``linux_monitor``, across every host you run, kiosks included; a panel that has
one already carries that entry beside this one. Keeping a second copy meant two
integrations reporting one fact from two transports onto one device page, which
is how a host ends up with two health sensors that disagree and an entity id
carrying a ``_2`` suffix nobody can remove. ``update.py`` (apt install) and
``drift.py`` (kiosk.sh assertions) are deleted rather than ported: the drift
they asserted cannot exist any more, because there is no second configuration
file to drift from — the agent's settings map is the panel's configuration.

The only numbers here are properties of a rule. A version, a count or a host
table is read live, never remembered.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "kiosk_pi"

# --- configuration keys -----------------------------------------------------

CONF_HOST = "host"
CONF_PORT = "port"
CONF_PASSWORD = "password"
CONF_SSL = "ssl"
CONF_VERIFY_SSL = "verify_ssl"
CONF_MAC = "mac"

# Options, per entry.
CONF_ALLOW_CONTROL_BOARDS = "allow_control_boards"
CONF_OFFLINE_EXPECTED = "offline_expected"

DEFAULT_PORT = 2323

# --- polling ----------------------------------------------------------------

# One HTTP round trip against a Pi on the LAN. Fast enough that a wall pointed
# somewhere new reads back within a refresh, cheap enough that four panels are
# not a measurable load on either end.
UPDATE_INTERVAL = timedelta(seconds=30)

# The agent reads every instrument it has for `deviceInfo`, and a Pi 3B doing
# that while Chromium is painting is not instant. Generous, but bounded: an
# unbounded read is a coordinator that never finishes and a wall that reports
# neither up nor down.
REQUEST_TIMEOUT = 15

# A SHORTER TIMEOUT FOR THE CONFIG FLOW than for the coordinator, deliberately.
# Somebody is sitting in front of the dialog, and a wrong address should say so
# rather than spin for fifteen seconds looking like the integration is broken.
SETUP_TIMEOUT = 8

# --- the browser-restart dwell ----------------------------------------------

# A restart takes the browser away and brings it back, so `currentURL` reads
# None for a few seconds either side of one. Publishing that as "the wall is
# showing nothing" makes every deliberate restart look like an outage, so the
# last known page is held for this long before the sensor lands on None.
#
# FALL DWELL, NEVER RISE DWELL. A page appearing is published at once; only its
# disappearance waits. Dwelling on the rise would delay the one reading an
# operator is watching for after pointing a wall somewhere new.
CURRENT_PAGE_DWELL = timedelta(seconds=45)

# --- external dashboard apps ------------------------------------------------

EXTERNAL_APPS_STORE_KEY = f"{DOMAIN}.external_apps"
EXTERNAL_APPS_STORE_VERSION = 1

# Seeded ONCE into the Store on first read and never re-seeded — re-seeding an
# empty list would resurrect an app somebody deliberately removed, which is
# indistinguishable from the removal never having taken. Empty by default: a
# board's address is deployment inventory and does not belong in a public
# repository's source.
DEFAULT_EXTERNAL_DASHBOARD_APPS: tuple[dict[str, object], ...] = ()

# How long an app's reachability probe may take. Short: it runs once per app
# per refresh of the select's options, and a board server that is down should
# drop out of the picker quickly rather than hold the refresh open.
APP_PROBE_TIMEOUT = 4

# --- settings keys on the agent ---------------------------------------------
#
# Named here rather than spelled inline at each call site so a rename on the
# agent is one edit on this side. These are the agent's own key names; see
# `agent/pikioskd/settings.py` for the declared set and its types.

SETTING_START_URL = "startURL"
SETTING_SCREEN_BRIGHTNESS = "screenBrightness"
SETTING_SCREENSAVER_BRIGHTNESS = "screensaverBrightness"
SETTING_SCREEN_OFF_TIMER = "timeToScreenOffV2"
SETTING_SCREENSAVER_TIMER = "timeToScreensaverV2"
SETTING_KIOSK_MODE = "kioskMode"
SETTING_MAINTENANCE_MODE = "maintenanceMode"
SETTING_ERROR_URL = "errorURL"

# 0 means "never" on both timers, matching the agent and matching Fully. A
# number entity therefore has to permit 0 and must not treat it as unset.
TIMER_NEVER = 0
TIMER_MAX_SECONDS = 86_400
BRIGHTNESS_MAX = 255
