# kiosk-pi 1.0.0 — architecture and data flow

What the pieces are, which way the data moves, and where each decision is
actually enforced. The 0.7.0-era documents this replaces described the ssh and
Glances transport, `kiosk.sh` drift detection and `sensor.<host>_kiosk_url`;
**none of that survives 1.0.0** and they were retired from `jrackerby/HA`
rather than updated, because a document describing a transport that no longer
exists is worse than no document.

This file describes structure and flow. It deliberately carries **no fleet
inventory** — no hostnames, no addresses, no panel count. Which panels exist is
live state, read off the registry; a list here would be wrong by the next
reimage and would publish estate inventory from a public repository.

---

## 1. The two halves

```
        ┌──────────────────────── the Raspberry Pi ─────────────────────────┐
        │                                                                   │
        │   pikioskd  (systemd, stdlib only, no dependencies)               │
        │     ├── BrowserSupervisor ──── cage ──── Chromium ──── the glass  │
        │     │         │                                                   │
        │     │         └── CDP over the DevTools port (localhost)          │
        │     ├── Display    (wlr-randr │ vcgencmd │ sysfs backlight)       │
        │     ├── device.py  (procfs, sysfs, vcgencmd, nmcli)              │
        │     ├── Settings   (one JSON file, atomic replace)                │
        │     └── HTTP server on :2323, ?cmd=… &password=…                  │
        └───────────────────────────────┬───────────────────────────────────┘
                                        │  HTTP, LAN, one round trip
                                        │  Authorization header, never a query
        ┌───────────────────────────────┴───────────────────────────────────┐
        │   custom_components/kiosk_pi   (inside Home Assistant)            │
        │     ├── KioskPiClient      (aiohttp, injected websession)         │
        │     ├── KioskPiCoordinator (one per panel, 30s)                   │
        │     ├── entity.py          (two base classes, see §5)             │
        │     ├── surface.py         (board-filter policy, pure)            │
        │     ├── external_apps.py   (the app list, in a Store)             │
        │     └── 9 platforms ───────► the entities on the device page      │
        └───────────────────────────────────────────────────────────────────┘
```

**The agent owns the browser.** It is not a control daemon bolted onto somebody
else's `kiosk.service`: it launches the compositor and the browser itself and is
the only thing that does. That is the whole reason it exists. A wall whose start
URL lives in a shell script and whose live URL lives in a browser has two
sources of truth, they drift, and the drift is only ever discovered by looking
at the glass. Here the settings file **is** the configuration and the browser is
started from it, so "what is this panel pointed at" has one answer.

**The agent has no dependencies at all.** Standard library only, enforced by
`agent/tests/test_stdlib_only.py`, which fails the build if that changes. A Pi
that cannot reach PyPI must still be able to run the thing that drives its
screen.

---

## 2. The transport

One HTTP server, one query-string command vocabulary, one shared secret
generated **on the device by its installer**.

| | |
|---|---|
| Port | `remoteAdminPort`, 2323 by default |
| Auth | a password the installer generates per host |
| Commands | `?cmd=status`, `?cmd=deviceInfo`, `?cmd=listSettings`, plus the setters |
| Framing | JSON, with a `status` key the integration strips before publishing |

Three properties of this transport are load-bearing and each is enforced in
code rather than by convention:

- **The credential travels in a header, never in the query string.** `str(url)`
  reaches aiohttp's own debug log, every exception's `str()`, and any
  diagnostics dump that includes the request.
- **The agent redacts the password out of its own request log.** Overriding
  `log_message` does not do it — `log_request` builds its line from
  `self.requestline`, already interpolated, so a redaction applied to the format
  string never sees the value. `server.py`'s `safe_path()` redacts the VALUE and
  both hooks route through it.
- **The reply is parsed, not content-type-sniffed.** `response.content_type` is
  absent when the server sends no header, carries the charset when it does, and
  is rewritable by any middlebox. A captive portal's login page and a router's
  404 are identified by failing to parse, and their body is what names them.

**Discovery offers, it does not configure.** `manifest.json` declares a DHCP
block on the hostname pattern, so a new panel appears in the UI with its address
pre-filled — and stops there. The agent's password is generated on the device
and is not discoverable, so a flow that completed unaided would have to be using
a shipped default, which would mean the fleet has one.

---

## 3. One refresh, two calls, one picture

```
  every 30s ──► asyncio.gather( deviceInfo , listSettings )
                      │              │
                      │              └── what a CONTROL needs to render its
                      │                  own position: both timers, both
                      │                  brightnesses, the error URL
                      └── what a READING needs: browser, screen, host,
                          network, the current page
                      │
                      ▼
              either fails → the whole refresh fails
                      │
                      ▼
              currentURL passes through the fall dwell (§4)
                      │
                      ▼
              coordinator.data + coordinator.settings
```

**One poll, not six.** `deviceInfo` returns the whole state of the panel in a
single call, so every entity on the device page is reading the same instant. A
coordinator that fetched the screen, the browser and the settings separately
would report three independent failure modes and would produce a torn read
whenever the wall changed mid-poll — the select showing the new board while the
current-page sensor still showed the old one, with nothing to say which is
right.

**The settings call is separate, deliberately.** Folding it into `deviceInfo`
would mean the agent serving its whole configuration on the polling path, every
30 seconds, for values that only change when somebody changes them. The two are
gathered concurrently and treated as **one** refresh: if either fails the
refresh fails, because half a picture is the torn read again.

**This coordinator raises `UpdateFailed`.** That is the quality scale's
`entity-unavailable` rule and it governs here — this coordinator reads a
DEVICE, which can be unreachable, so an entity that kept publishing its last
value would be asserting something about a panel nobody can see. The estate's
never-raise contract governs a coordinator reading other ENTITIES, which has no
device to lose, and it is not contradicted by this.

### Failure dispositions

| What happened | What the coordinator does | Why |
|---|---|---|
| 401 from the agent | `ConfigEntryAuthFailed` → reauth flow | A wrong password will still be wrong in 30 seconds. Retrying forever fills the agent's log with 401s nobody connects to an HA entry. |
| Cannot reach the host | `UpdateFailed`, INFO once at the crossing | Nobody can act on it but the network; the entity going unavailable **is** the report. |
| Agent answered an error | `UpdateFailed`, INFO once at the crossing | Same split: the integration reporting on its own subject. |
| `offline_expected` option on | Returns `{"offlineExpected": True}`, dials nothing | A wall unplugged for the summer should stop filling the log without its entry — and its history — being deleted. |

**The once-only log is deduped on a stable condition token, never on the
message.** A message carrying the error text turns one condition into a new line
on every poll. The moving figure belongs on the entity, where a surface reads it.

---

## 4. The dwells

There is exactly one, and its direction is the whole of the design.

**`CURRENT_PAGE_DWELL`, 45 seconds, FALL ONLY.** A browser restart takes the
page away and brings it back, so `currentURL` reads `None` for a few seconds
either side of one. Publishing that as "the wall is showing nothing" makes every
deliberate restart look like an outage.

```
   page appears  ──────────────────────────────► published immediately
   page vanishes ──► hold last known ──45s──► published as None
```

**Rise dwell would be the wrong half.** The reading an operator is watching for,
having just pointed a wall somewhere new, is the page APPEARING — delaying that
one to smooth the other trades the useful signal for the cosmetic one. Losing
sight of a source counts as a fall; gaining one does not.

**The dwell's state lives on the coordinator, not on the sensor**, so two
consumers of the same reading cannot dwell differently.

---

## 5. The entity surface

Nine platforms. Two base classes, and which one an entity takes is a real
decision:

- **`KioskPiEntity`** — unavailable when the coordinator is unavailable.
- **`KioskPiBrowserEntity`** — additionally unavailable while the BROWSER is
  down, because a control that is present and cannot work teaches an operator
  that a red toast is normal.

**One entity opts out of the coordinator's unavailability.**
`binary_sensor.<panel>_agent` describes THE READING ITSELF rather than the
panel, so a version of it that disappeared with its subject could never report
the subject down, and the device page would go blank rather than say what
happened. It overrides `available` to ignore the refresh entirely and publishes
`last_update_success` as its state — **with exactly one exception**: it *does*
report unavailable under `offline_expected`, because a panel deliberately
switched off is not a connectivity fault, and publishing it as one trains an
operator to ignore the one entity that should never be ignored.

**Everything else maps an unreadable value to unavailable, never to off.** A
description's `value_fn` returns `None` for exactly that, and collapsing it to
`False` would throw away the distinction it was written to preserve: `ok at
zero` and `could not read` are different values at the source.

**What is deliberately absent, and why it is absent here rather than merely
unbuilt:** apt state, kernel versions, pending updates, an `update` entity, a
reboot-host button, the host's IP address and its uptime. `linux_monitor` and
`cyber_estate` own generic OS health for every host in this estate, and each
panel already carries an entry from both. Shipping duplicates put every one of
them in the registry as a `_2` beside the owner's, permanently, because the
registry never frees an id. Chromium's version **does** belong here — that is
the browser this integration exists for, not a system package — and so does
`throttle`, read from the Pi's own firmware.

**ONE HOST, ONE PATCHER.** The same line settles the installer: `install.sh`
does not arm `unattended-upgrades` and does not write the apt fleet standard's
four artefacts, because patching is driven from Home Assistant by
`linux_monitor`'s `update.<host>_system_updates` — which reports, offers
Install, and owns the reboot that follows. Two patchers on one machine race for
the same dpkg lock, and the HA-side Install reports a failure it did not cause.
If that ever reverses, the four artefacts and the drift check that watched them
come back together; the standard was only ever safe because something watched
it.

**The counts, not the states, are the signals.** `browser_restarts` is a
`TOTAL_INCREASING` sensor because a crash-looping Chromium under
`Restart=always` reports `active` on every poll that lands between crashes;
`systemctl is-active` proves nothing about a browser, and the restart count is
the only reading that moves.

**`vcgencmd get_throttled`'s high half is sticky** — bits 16–19 are since-boot,
so a wall that browned out at 3am still reports it at noon. An unreadable
bitmask is `unknown`, never clean: a throttle report defaulting to healthy is
exactly the reading a browning-out panel would give.

---

## 6. The board picker, and what a wall may be pointed at

```
  external_apps.py (Store)        ── the operator's app list, seeded once,
        │                            never re-seeded, empty by default
        ▼
  discovery.async_refresh_cache   ── probes each app, 4s each, drops what
        │                            does not answer
        ▼
  surface.allowed_apps(...)       ── the policy, pure, imports no HA
        │
        ▼
  select.<panel>_dashboard        ── options AND async_select_option both
                                     call the same accessor
```

**The predicate is "not control", never "is monitor".** Monitor, ambient, health
and anything else non-control are all legitimate on a wall; only control is not,
because an ambient panel is a screen with no keyboard, mouse or touch — a hover
never fires and anything behind a tap is hidden for ever. An is-monitor test
would strip a health board out of the picker of the very wall currently showing
one.

**An unclassifiable app is treated as control, i.e. filtered out.** That is the
restrictive default and it is the right one *here*, even though the shell that
renders these boards defaults the other way for its own purposes: a shell that
cannot classify a board should still render it, and a picker that cannot
classify a board should not offer it to a screen nobody can touch.

**The surface is DECLARED, never inferred from the URL.** A pathname ladder
guessing from words like "panel" or "monitor" classifies by spelling — right
until somebody names a control board `wall-monitor`, at which point it is
confidently wrong with nothing in the data to catch it.

**The filter lives in the integration, not in a card.** A card-side guard leaves
the board reachable from more-info, from an automation and from voice.

**Both the select's `options` and its `async_select_option` go through the one
accessor**, because a config key read by two code paths that classifies by one
and lays out by another can silently lose state.

---

## 7. The two services

Registered in `async_setup`, **not** in `async_setup_entry`: registering per
entry would tear them down when the FIRST panel is removed, leaving the
remaining panels with automations that fail on a service that no longer exists.

| Service | What it does | Lifetime |
|---|---|---|
| `kiosk_pi.load_url` | Points a panel at a URL now | **Transient.** Survives until the next restart or start-URL load; it does not rewrite `startURL`. |
| `kiosk_pi.set_config` | Writes agent settings | Persistent, via the agent's atomic replace |

**A target that is not a loaded panel is a `ServiceValidationError`, never a
silent skip** — a skip reports success while three of four targets did nothing.
A device-side failure is a `HomeAssistantError` carrying the agent's own message.

---

## 8. Where the browser is actually driven

CDP over the DevTools port, and every one of these is a trap that has already
been paid for once:

- **A failed navigation leaves the attempted URL in the target list**, so a wall
  showing "site cannot be reached" reports the board's address and reads
  healthy. The interstitial is detected on `#main-frame-error` via
  `Runtime.evaluate`, never on the URL.
- **An interstitial does not know its own address.** Inside Chromium's network
  error page `location.href` is `chrome-error://chromewebdata/` — the attempted
  URL is exposed only to the TARGET. Read the code from the document and the URL
  from `/json/list`.
- **CDP multiplexes events onto the command socket**, so the reply to command N
  is not the next message. Match on `id`.
- **`webSocketDebuggerUrl` is minted per target.** Rediscover per call.
- **Client frames must be masked**, or Chromium answers with a close rather than
  an error anybody can read.
- **Signal the process group, not the leader.** `cage` forks Chromium and
  Chromium forks a zygote and a renderer per tab; signalling the leader alone
  leaves orphans holding the DRM device, after which the next `cage` cannot take
  it and the wall stays black with no error anybody sees.

---

## 9. The settings file, and the write contract

One JSON file, typed by a declared spec table, written by **atomic replace** —
temp file beside the target, fsync, rename. That needs write permission on the
DIRECTORY, not on the file, and getting that wrong fails only on restart.

**An in-memory read-back is not a read-back.** The agent's contract is that
every write reports the STORED value. When a persist failed it reported from
memory instead: the map was already updated, `listSettings` answered with the
new value, the coordinator polled it as correct, the panel behaved as if it had
taken, and the change vanished at the next restart. **The write rolls memory
back when `save()` raises**, or all-or-nothing is a claim the code does not
honour. Permissions were only the trigger — a full SD card or an ext4 that has
flipped read-only produces it identically, which is why this fleet carries a
sensor for the latter.

**Some settings do not take until the browser restarts** — the CDP port, the
binaries, the flags, the profile and cache directories, `xdgRuntimeDir`,
`kioskMode`, `hideCursor`. The setter that touches one says so in its reply,
rather than letting the caller conclude from a 200 that the wall has already
moved.

---

## 10. The systemd unit is part of the design

Two sandbox directives have each killed a function of this service while the
unit read healthy, and the unit is therefore joined to the code by tests rather
than left as deployment detail.

- **`ProtectHome=read-only` kills the browser and the unit reads
  `active (running)` with `NRestarts=0`.** Every path under `/home` becomes
  unwritable, so a Chromium pointed at a `--user-data-dir` there exits **21**
  about a second into every launch — and `Restart=always` governs the AGENT,
  which is fine. The profile lives in the unit's `StateDirectory`
  (`/var/lib/pikioskd`), and `tests/test_unit_sandbox.py` joins the unit's
  writable set to the settings default so the two cannot drift apart again.
- **`NoNewPrivileges=yes` forbids `sudo` entirely, silently.** It sets
  `PR_SET_NO_NEW_PRIVS`, which stops a setuid binary elevating at all; sudo does
  not degrade, it refuses. A no-password sudoers drop-in grants nothing while it
  is on. This hit `rebootDevice`, whose privileged command runs in a DETACHED
  shell — so the API answered `{"rebooting": true}`, the reply was already sent,
  and the panel never rebooted. **Prove the privilege with a cheap `sudo -n
  true` before promising the action**, never with the real command.

Both have the same shape: a sandbox directive forbidding the one thing the
service exists to do, reported by nothing.

---

## 11. What this cannot tell you

Stated so the next reader does not go looking:

- **Whether the cursor is hidden on a given wall.** A CDP screenshot never
  contains the compositor cursor, so `image.<panel>_screenshot` cannot answer
  it. That needs `grim -c` on the host, or eyes on the glass.
- **Which board a panel is *supposed* to show.** This repository publishes what
  it IS showing — `sensor.<panel>_current_page`, read from the browser through
  CDP — and `text.<panel>_start_url`, which is what it will show after a
  restart. Neither is an assignment: nothing here holds a table of which wall
  ought to carry which board, and a document that grew one would be inventory
  going stale in public.
- **Brightness on an HDMI monitor.** There is no `/sys/class/backlight` device
  for one, so brightness is genuinely unavailable and is reported as absent
  rather than as a number nothing controls.
- **Which panels exist.** Registry, not documentation.
