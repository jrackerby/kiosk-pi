# pikioskd

The kiosk agent that runs on the Raspberry Pi. It launches Chromium under
`cage`, supervises it, owns the panel's configuration, and serves a
Fully-Kiosk-shaped HTTP admin API.

Home Assistant is one client of that API — the integration in
`../custom_components/kiosk_pi/` — but nothing here depends on Home Assistant.
`curl` drives it fine.

## Design, in three claims

**The agent owns the browser.** It is not a control daemon bolted onto somebody
else's `kiosk.service`. A wall whose start URL lives in a shell script and
whose live URL lives in a browser has two sources of truth, they drift, and the
drift is only ever found by looking at the glass. Here `settings.json` is the
configuration and the browser is started from it.

**Standard library only, and it is a requirement rather than a preference.** A
panel is headless with no input devices, so a failed dependency install during
an upgrade is a black screen recoverable only over SSH. Debian enforces PEP 668,
which makes every dependency a venv, a wheel build for ARM and a provisioning
step that can fail. `tests/test_stdlib_only.py` fails the build if this stops
being true — statically, by AST, because an import-time check passes on the one
machine whose opinion does not matter.

**Every action reads back what it claims to have changed.** A command returning
`OK` only says the call was dispatched. The reply carries the observed state.

## Install

```bash
sudo agent/install.sh
```

See the [repository README](../README.md#install) for what it does and how to
roll back. It is idempotent; re-run it to upgrade.

## Files

| path | what |
|---|---|
| `/opt/pikioskd/pikioskd/` | the code |
| `/etc/pikioskd/settings.json` | the panel's entire configuration, mode 0640 |
| `/etc/systemd/system/pikioskd.service` | the unit |
| `/etc/sudoers.d/pikioskd` | one verb: `systemctl reboot` |
| `/etc/udev/rules.d/99-pikioskd-backlight.rules` | backlight writable by `kiosk` |

`settings.json` plus the unit fully describe a panel. There is no second store
and nothing derived kept beside it, which is what makes a wall reprovisionable
from this repository rather than from whatever was typed into it once.

## The API

`GET http://<panel>:2323/?cmd=<command>&password=<password>` — or the same
command with `Authorization: Bearer <password>`, which keeps the secret out of
every log that prints a URL. `POST` is accepted for the same commands, with a
form-encoded or JSON body.

Answers `{"status": "OK", ...}` or `{"status": "Error", "statustext": "..."}`.
**The HTTP status code carries the verdict**: 200 fine, 400 bad argument, 401
bad password, 403 remote administration switched off, 404 unknown command, 502
the agent is fine and something it depends on is not.

### Reads

| command | |
|---|---|
| `status` | cheap liveness; touches no hardware |
| `deviceInfo` | everything about the panel, in one call |
| `listSettings` | the whole settings map, typed |
| `getCurrentURL` | what the browser is painting |
| `getScreenshot` | PNG bytes |

Read-only commands **do not reset the idle clock**, so a Home Assistant
coordinator polling every 30 seconds cannot hold the screensaver off for ever.

### Writes

| command | arguments | |
|---|---|---|
| `loadURL` | `url` | navigate now; does *not* change `startURL` |
| `loadStartURL` | | back to the configured board |
| `restartApp` | | restart the browser |
| `toForeground` | | raise the page |
| `clearCache` / `clearCookies` | | through the browser, not by deleting a directory |
| `setOverlayMessage` | `text` | full-screen message over the live page; empty clears |
| `screenOn` / `screenOff` | | DPMS, not a black page |
| `setBrightness` | `level` (0–255) | needs a backlight device |
| `startScreensaver` / `stopScreensaver` | | |
| `setStringSetting` | `key`, `value` | coerces to the key's declared type |
| `setBooleanSetting` | `key`, `value` | refuses a non-boolean key |
| `setIntSetting` | `key`, `value` | refuses a non-integer key |
| `rebootDevice` | | scheduled a second out, so the reply leaves first |

```bash
curl -s "http://panel.local:2323/?cmd=deviceInfo&password=$PW" | python3 -m json.tool
curl -s "http://panel.local:2323/?cmd=loadURL&password=$PW&url=http://boards/alert/"
curl -s "http://panel.local:2323/?cmd=setIntSetting&password=$PW&key=timeToScreensaverV2&value=900"
```

## Settings

Every key, its type and its default are declared in
[`pikioskd/settings.py`](pikioskd/settings.py); a key absent from that
declaration cannot be written, which stops a typo becoming a silently persisted
setting nothing reads.

The ones worth knowing:

| key | |
|---|---|
| `startURL` | what the panel comes back to |
| `errorURL` | outage page substituted on **any** failed load; empty disables |
| `timeToScreenOffV2` | seconds idle before the output is cut; **0 means never** |
| `timeToScreensaverV2` | seconds idle before dimming; **0 means never** |
| `screenBrightness` / `screensaverBrightness` | 0–255 |
| `kioskMode` | `--kiosk`; off makes it an ordinary browser window |
| `chromiumFlags` | extra flags, whitespace-separated |
| `outputName` / `rotation` | which output to drive, and its transform |
| `cdpPort` | DevTools port, 9222 by default |

Changing a key that the browser reads at launch restarts the browser
immediately, and the reply says so.

### The outage page contract

`errorURL` is loaded with `panel`, `back`, `url` and `error` appended to its
query, and any query the template already carried is preserved. That is the
same shape Fully's `errorURL` produces, so one outage page serves a mixed fleet
of Pi panels and Android tablets and says the same thing on both.

## Differences from Fully Kiosk

Same command vocabulary where it means the same thing. Three deliberate
deviations, each fixing something that has cost debugging time:

1. **Honest types.** Fully returns booleans as JSON booleans and most integers
   as *strings*, so `timeToScreenOffV2` reads `'0'` and a plain `!= 0`
   comparison calls every correctly configured device drifted, for ever. Here
   an integer reads back as an integer.
2. **One write path.** Writing a boolean through Fully's string setter answers
   200 and silently no-ops — and so does the mirror on some keys — so callers
   learned to write, re-read, and fall back to the other setter. Here the
   obvious call works and the reply says what was stored.
3. **Real status codes.** See above.

And what is simply absent, because a Pi is not an Android tablet: no battery,
no camera or motion detection, no audio or text-to-speech, no app management,
no MQTT.

## Tests

```bash
pip install -r requirements-test.txt
pytest
```

No Pi, no browser and no compositor needed: the parts with subtle failure modes
— the WebSocket frame codec, the settings coercion, `wlr-randr` parsing, the
throttle bitmask, the outage-page query — are pure functions over bytes or
text, and the HTTP surface is exercised over a real loopback socket against an
agent whose every instrument is absent. That last case is not a compromise; it
is the state a real panel is in between a crash and a restart, so the suite also
asserts that the API stays answerable when the wall is down.
