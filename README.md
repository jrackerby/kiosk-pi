<p align="center">
  <picture>
    <!-- The wordmark is dark grey and vanishes on a dark ground, and HACS
         renders this README inside Home Assistant's frontend, which is dark
         by default; a renderer that drops <source> lands on the <img>, so the
         dark-safe variant is the img and the light one the opt-in source. -->
    <source media="(prefers-color-scheme: light)" srcset="custom_components/kiosk_pi/brand/logo.png">
    <img src="custom_components/kiosk_pi/brand/dark_logo.png" alt="Kiosk Pi" width="420">
  </picture>
</p>

# Kiosk Pi

A kiosk browser for Raspberry Pi wall panels, and the Home Assistant
integration that drives it — Fully Kiosk's model, on hardware Fully does not
run on.

Two halves, versioned together in this repository:

| | what it is | where it runs |
|---|---|---|
| **`agent/`** | `pikioskd` — launches and supervises Chromium under `cage`, owns the panel's configuration, and serves a Fully-Kiosk-shaped HTTP admin API | on the Pi |
| **`custom_components/kiosk_pi/`** | the Home Assistant integration, an HTTP client of that agent | in Home Assistant |

## Why

A Pi wall panel is usually a shell script that execs a browser at a URL. That
works until you want to change what it shows, know what it is *actually*
showing, turn the screen off at night, or find out that Chromium has been
crash-looping for a week behind a `systemd` unit that still reads `active`.

Fully Kiosk solves all of that on Android, and its Home Assistant integration
is the model for what a kiosk should expose. This is the same idea for a Pi:
the agent owns the browser, so there is exactly one answer to "what is this
panel configured to show", and Home Assistant reads and writes it over HTTP.

## What it gives you

Per panel:

- **The surface** — `sensor.<panel>_current_page` reads what Chromium is
  *painting*, which is a different question from what it was told to paint;
  `text.<panel>_start_url` and `select.<panel>_dashboard` set what it comes
  back to; `image.<panel>_screenshot` shows it.
- **The screen** — on/off, brightness, a screen-off timer and a screensaver
  timer, all persisted on the device.
- **The browser** — running or not, its restart count, its version; restart it,
  flush its cache, bring it to the front.
- **A full-screen overlay message** (`notify.<panel>_overlay_message`) drawn
  *over* the live board rather than navigating away from it, so clearing it
  reveals a board that never lost its session.
- **An outage page.** Point `text.<panel>_outage_page_url` at a page of your
  choosing and any failed load lands there instead of on Chromium's own error
  screen — per device, needing no automation, surviving a reboot.
- **Enough host telemetry to explain a misbehaving panel** — CPU temperature,
  memory, storage, Wi-Fi signal, and the Raspberry Pi throttle bitmask, whose
  since-boot half means a wall that browned out at 3am is still saying so at
  noon.

## What this cannot do

Stated up front, because each one is a real limitation rather than a gap
waiting to be filled:

- **Brightness needs a backlight device.** `/sys/class/backlight` exists for a
  DSI or DPI panel and *not* for an HDMI monitor. On a panel driving a monitor
  the brightness entities report unavailable rather than moving a slider that
  changes nothing.
- **The overlay does not survive a navigation.** It is injected into the live
  document; there is no compositor surface above a fullscreen Chromium under
  `cage`. Re-issue it after repointing a wall.
- **"Idle" means "no command received".** A wall panel has no keyboard, mouse
  or touch, so the screensaver and screen-off timers count from the last
  command the agent was given. Read-only polling deliberately does not reset
  them.
- **No audio, no text-to-speech, no camera, no motion detection.** Fully has
  these because an Android tablet has them.
- **No OS health.** apt state, kernel upgrades and reboot-required belong to a
  general host monitor across every machine you run, not to a kiosk
  integration. Versions before 1.0.0 reported them here and 1.0.0 removes them;
  see *Upgrading to 1.0.0*.

## Install

### 1. The agent, on each Pi

Needs Python 3.11+, `cage` and `chromium`. It has **no Python dependencies** —
installing it is a file copy, deliberately, because a wall panel with no
console attached is a bad place for a failed dependency install.

```bash
git clone https://github.com/jrackerby/kiosk-pi.git
cd kiosk-pi
sudo agent/install.sh
```

The installer:

- copies the agent to `/opt/pikioskd` and installs `pikioskd.service`;
- writes `/etc/pikioskd/settings.json`, **seeding `startURL` from the `URL=`
  line of an existing `/home/kiosk/kiosk.sh`** if there is one;
- generates a random remote-admin password and **prints it once**;
- installs a sudoers drop-in granting exactly one verb — `systemctl reboot` —
  and a udev rule making the backlight writable by the `kiosk` user;
- **disables** any existing `kiosk.service` without deleting it, so rolling
  back is `systemctl disable --now pikioskd && systemctl enable --now kiosk`;
- verifies by calling the agent's own API back over the loopback, because
  `systemctl is-active` reads `active` for a service that is crash-looping.

Re-run it to upgrade. It leaves the settings file alone.

### 2. The integration, in Home Assistant

Via HACS: **HACS → ⋮ → Custom repositories →**
`https://github.com/jrackerby/kiosk-pi`, category **Integration**. Install,
restart Home Assistant, then **Settings → Devices & Services → Add Integration
→ "Kiosk Pi"**.

You need the panel's address and the password the installer printed. It is
stored at `/etc/pikioskd/settings.json` on the device if you need to read it
back.

Panels with a hostname matching `pi*kiosk*` are offered by DHCP discovery,
which pre-fills the address. It cannot pre-fill the password — that is
generated on the device, and a discovery flow that completed unaided would mean
the fleet shipped with a shared default.

## Configuration

**Per panel** (*Configure → This panel's options*):

- **Allow control boards** — a wall panel usually has no keyboard, mouse or
  touch, so a control board on it is unreachable: a hover never fires and
  anything behind a tap is hidden for ever. Leave this off unless the panel can
  actually be operated. It is a boundary, not a preference: it filters the
  dashboard picker *in the integration*, so a filtered board is unreachable
  from more-info, an automation and voice as well.
- **This panel is expected to be offline** — stops polling and stops reporting
  a fault, without deleting the panel and its recorded history.

**Dashboard apps** (*Configure → Add / Remove a dashboard app*) — the shared
list `select.<panel>_dashboard` offers. Each carries a name, a URL and a
declared *surface* (`control`, `monitor`, `health`, `ambient`). The surface is
declared rather than guessed from the URL: a ladder that reads the path is
right until somebody names a control board `wall-monitor`, at which point it is
confidently wrong and nothing in the data catches it.

An app is only offered while its own HTTP probe answers. That probe runs from
Home Assistant, which is not where the browser runs, so a failure drops an app
from the picker but never repoints a wall.

## Actions

`kiosk_pi.load_url` navigates a panel **now**, without changing what it comes
back to. That is deliberate: an automation that flips a wall to a camera feed
for thirty seconds must not silently re-provision the panel — you would find
out at the next reboot, by which point nobody connects the two. Use the
dashboard select or the start-URL text entity to change a panel permanently.

`kiosk_pi.set_config` writes one of the agent's own settings by name, for
anything without an entity yet. The value is coerced to the setting's declared
type and refused if it does not fit.

## The agent's HTTP API

`http://<panel>:2323/?cmd=<command>&password=<password>`, or the same with
`Authorization: Bearer <password>`. Answers JSON with a `status` of `OK` or
`Error`. Full command list in [`agent/README.md`](agent/README.md).

Three things it does differently from the APIs it is modelled on, each fixing
something that has actually cost debugging time:

- **The status code carries the verdict.** An unknown command is 404, a bad
  password 401, a bad argument 400, a device-side failure 502. An API that
  answers 200 to everything and puts the verdict in the body reads a guessed
  command name as live, and a fleet audit passes over devices that understood
  nothing.
- **Types are honest.** A setting declared as an integer reads back as an
  integer. Fully returns most integers as strings, so `timeToScreenOffV2` reads
  `'0'` and a plain `!= 0` calls every correctly configured device drifted,
  for ever.
- **One write path.** Writing a boolean through the string setter coerces
  rather than answering 200 and silently no-opping, so there is no write /
  re-read / fall-back-to-the-other-setter dance.

## Upgrading to 1.0.0

**Your entry, its device, its entities and their history all survive. One
thing does not carry across: the password.**

Versions before 1.0.0 drove a panel over SSH with a key and a username. 1.0.0
drives the agent over HTTP with a password *generated on the device by the
agent's installer* — so at migration time that credential genuinely does not
exist yet. Everything else about the entry is computable and is migrated.

The entry therefore comes up asking to be reconfigured:

1. Run `sudo agent/install.sh` on the panel and note the password it prints.
2. Answer the reconfigure prompt Home Assistant has already raised for that
   panel with the password.

**Do not add the panel again from *Add Integration*, and do not delete the
entry.** The migrated entry still holds the panel's identity, so adding it
again is refused as `already_configured`; deleting it would take the device,
the entities and every recorded row behind them. Reconfiguring keeps all of it.

**Entities that no longer exist.** The `update.<host>_system` entity (apt
upgrade), `binary_sensor.<host>_config_drift`, and the apt/kernel/dmesg/NIC
sensors are gone. Generic OS health belongs to a host monitor across every
machine you run; keeping a second copy here put two integrations on one device
page reporting one fact from two transports. The drift sensor is not merely
moved but *retired*: it asserted that a panel's `kiosk.sh` matched a fleet
standard, and there is no second configuration file to drift from any more —
the agent's settings map is the panel's configuration.

## Removing it

Delete the config entry in Home Assistant (**Settings → Devices & Services →
Kiosk Pi → ⋮ → Delete**). That removes the device, its entities and their
history.

On the panel:

```bash
sudo systemctl disable --now pikioskd
sudo rm -rf /opt/pikioskd /etc/pikioskd /etc/systemd/system/pikioskd.service \
            /etc/sudoers.d/pikioskd /etc/udev/rules.d/99-pikioskd-backlight.rules
sudo systemctl daemon-reload
```

If you had a `kiosk.service`, `sudo systemctl enable --now kiosk` puts it back —
the installer disabled it rather than deleting it for exactly this.

## Troubleshooting

**The integration cannot connect.** `systemctl status pikioskd` and
`journalctl -u pikioskd -n 50` on the panel. The agent refuses *every* request
while `remoteAdminPassword` is empty, and says so on every start.

**The panel is up but the browser is down.** `binary_sensor.<panel>_browser` is
off and `sensor.<panel>_browser_restarts` is climbing. The agent backs its
restarts off exponentially, so a panel that cannot start does not spend its SD
card's remaining write cycles finding out. The count is the signal — a
crash-looping browser under any restart-always supervisor reads `active`
for ever.

**The wall is showing an outage page.** `sensor.<panel>_current_page` will say
so, carrying the failed URL and Chromium's own error code in its query string.
The board server is down; the panel is fine.

**Brightness does nothing.** See *What this cannot do*. If the panel *does*
have a backlight, check the udev rule landed:
`ls -l /sys/class/backlight/*/brightness` should show group `video` and be
group-writable.

**Everything reads unavailable but the panel is plainly working.** Check
`binary_sensor.<panel>_agent`. It is the one entity that stays available when
the refresh fails, because a monitor that disappears with its subject cannot
report the subject down.

## Development

```bash
pip install -r agent/requirements-test.txt && (cd agent && pytest)   # agent
pip install -r requirements-test.txt && pytest                       # integration
```

Two suites, two Python versions, two dependency sets. The agent's runs on 3.11
— the oldest it claims to support — and needs nothing but pytest;
`agent/tests/test_stdlib_only.py` fails the build if the agent ever grows a
third-party import, because that check cannot be left to a README line: a
dependency added here does not fail in CI, it fails on a Pi during an upgrade.

How the pieces fit together — the agent, the coordinator, the transport, the
failure dispositions, the one dwell and its direction — is
[`docs/architecture.md`](docs/architecture.md). Traps that have bitten in this
repository are recorded in [`TOOLS.md`](TOOLS.md).

Issues and feature requests:
[jrackerby/kiosk-pi/issues](https://github.com/jrackerby/kiosk-pi/issues).

Pushing a `custom_components/kiosk_pi/manifest.json` whose `version` has
changed tags and publishes a release automatically. That is the only supported
way to cut one.
