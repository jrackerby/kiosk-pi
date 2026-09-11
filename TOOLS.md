# TOOLS

What the instruments this repository owns actually do, refuse to do, and lie
about. Every line is a claim with a timestamp — re-verify before building a
plan on one, and edit it when it stops being true.

A trap belongs to whichever repository holds the instrument it is about. This
file covers the Pi panel agent and the two test harnesses that exercise it and
the integration. Traps about Home Assistant's own instruments are not this
repository's to record.

## `BaseHTTPRequestHandler` logging

- **OVERRIDING `log_message` DOES NOT STOP THE PASSWORD REACHING THE LOG, and
  it looks like it does.** `log_request` builds its own line from
  `self.requestline` and hands it down already interpolated, so a redaction
  applied to the format string never sees the value. The request line contains
  every query parameter, so a remote-admin API authenticated by
  `?password=` writes its shared secret to the journal on every single poll.
  Redact the VALUE — `server.py`'s `safe_path()` — and route both `log_request`
  and `log_message` through it. Caught only by asserting on captured log text;
  the obvious override reads correct.
- `sys_version = ""` on the handler, or every reply advertises the Python
  version to the LAN in its `Server` header.

## `aiohttp` client

- **`response.content_type` IS NOT A USEFUL GATE ON "IS THIS JSON".** It is
  absent when the server sends no header, it carries the charset when the
  server sends one, and a reverse proxy in front of the device can rewrite it.
  A `!= "application/json"` check refuses a perfectly good reply, and refuses
  it as a *command* failure — pointing an operator at the device rather than at
  the middlebox. Parse with `response.json(content_type=None)` and let the
  parse failure be the finding: a captive portal's login page and a router's
  404 both fail to parse, and their body is what identifies them.
- Put a bearer credential in the **header**, never the query string. `str(url)`
  reaches aiohttp's own debug log, every exception's `str()`, and any
  diagnostics dump that includes the request.

## `pytest-homeassistant-custom-component`

- **ONE `async_block_till_done()` AFTER `async_fire_time_changed` IS NOT
  ENOUGH, AND THE UNDER-DRAINED CASE LOOKS LIKE A PRODUCTION BUG.** The first
  drain gets as far as the request being ISSUED — `aioclient_mock.mock_calls`
  already shows it — while the mocked response resolves on a later loop turn,
  so the coordinator is still holding its previous data. A test written that
  way reads as a coordinator that fetched and ignored the answer, which is
  indistinguishable from a real caching bug and was mistaken for one here.
  Measured on this harness: settled after the **third** drain. `tests/`'s
  `advance()` helper is the only place that is encoded.
- `asyncio_mode = auto` in the pytest config, or every test using the `hass`
  fixture errors with "requested an async fixture with no plugin that handled
  it" — which reads like a broken fixture rather than a missing setting.
- The harness pins its own Home Assistant, which lags the version a live
  instance runs. Green here is not proof against the running instance; it is
  proof against the version in `requirements-test.txt`.
- `aioclient_mock` matches on the **full URL including the query**. A mock
  keyed on the path alone answers `listSettings` with a `deviceInfo` payload
  and every assertion downstream still passes.
- **THE TWO SUITES CANNOT SHARE A VENV.** This harness pulls in
  `pytest-socket`, which blocks every `socket.socket` for the whole session,
  and `agent/tests/test_server.py` binds a real loopback server — so the
  agent suite errors with `SocketBlockedError` on every server test while
  its own `requirements-test.txt` is blameless. `-p no:socket` does not
  clear it. CI never sees this because each suite is its own job with its
  own install; locally, one venv per suite. Measured 2026-09-11.

## `hassfest` and the quality scale

- **`hassfest` NEVER CHECKS A CUSTOM COMPONENT AGAINST THE QUALITY SCALE.**
  `validate_iqs_file` opens with `if not integration.core: return`, so
  `quality_scale.yaml` goes unread — while `manifest.json`'s schema still
  ACCEPTS a `quality_scale` key. A tier declared there is a self-claim with no
  gate behind it, green for ever. Read the rule list from `ALL_RULES` in
  `home-assistant/core`'s `script/hassfest/quality_scale.py`, never from the
  docs page, which names the tiers and not the rules.
- `home-assistant/actions/hassfest` takes **no path input**: it scans
  `custom_components/*` at the workspace root. Handed a repository laid out any
  other way it finds zero integrations and reports green over nothing.

## HACS

- **HACS fetches `hacs.json` and `manifest.json` from
  `raw.githubusercontent.com` with NO `Authorization` header**, in CI and in a
  live install alike, so a private repository 404s there and the failure
  presents as "invalid hacs.json". That bites a real install, not just CI.
- `hacs/action`'s validators gate DEFAULT-STORE INCLUSION, not whether a
  custom-repository install works: `async_run_repository_checks` returns
  immediately unless `hacs.system.action`.
- `brands` can never pass from a custom repository — `home-assistant/brands`
  takes core integrations only.

## Chromium DevTools on a panel

- **AN INTERSTITIAL DOES NOT KNOW ITS OWN ADDRESS.** Inside Chromium's network
  error page `location.href` is `chrome-error://chromewebdata/` — the attempted
  URL is not exposed to the document at all, only to the TARGET. A detector that
  reads the page therefore identifies the fault correctly and then reports it
  against nothing, which is how an outage notice shipped with
  `back=chrome-error://chromewebdata/`: a dead link on a page whose whole job is
  to say which board is down. Read the code from the document and the URL from
  `/json/list`.
- **NO CONNECTED OUTPUT MEANS NO CDP, WHILE THE BROWSER READS RUNNING.** With
  `/sys/class/drm/card0-HDMI-A-1` `disconnected`, `cage` starts, Chromium
  starts, the process table is full, `browserRunning` is `true` and the
  restart count is 0 — and `:9222` is never opened, so `currentURL`,
  `cursorStyle` and the monitor all read `None`. On the HA side that is
  `current_page` unknown, `monitor` unknown and `cursor_hidden` unavailable
  on a panel whose browser is `on`. Read the DRM status before diagnosing
  the browser; it is a bench host with nothing plugged in. Measured on the
  bench panel, 2026-09-11.
- **`/json/list`'s key ORDER IS NOT A CONTRACT.** Parse it as JSON. A pattern
  assuming `type` precedes `url` reads the wrong field the day Chromium
  reorders them, and reads it confidently.
- **`webSocketDebuggerUrl` IS MINTED PER TARGET** and changes when the target
  does, so a cached socket URL points at a tab that no longer exists. Rediscover
  per call.
- **A FAILED NAVIGATION LEAVES THE ATTEMPTED URL IN THE TARGET LIST**, so a wall
  showing "site cannot be reached" reports the board's address and reads
  healthy. Detect the interstitial on `#main-frame-error` via
  `Runtime.evaluate`, never on the URL.
- **CDP MULTIPLEXES EVENTS ONTO THE COMMAND SOCKET.** The reply to command N is
  not the next message — a `Page.navigate` produces a burst of lifecycle events
  either side of its own result. Match on `id`.
- **A CONTROL FRAME MAY ARRIVE BETWEEN THE FRAGMENTS OF A DATA MESSAGE.** A
  reader that handles pings only before its assembly loop splices the ping's
  bytes into the payload, which then fails to parse as JSON and reads like a
  browser bug.
- **CLIENT FRAMES MUST BE MASKED.** Chromium answers an unmasked one with a
  close, not with an error anybody can read, so it presents as "the browser hung
  up".

## The panel itself

- **A SYSTEMD SANDBOX DIRECTIVE CAN KILL THE BROWSER WHILE THE UNIT READS
  HEALTHY.** `ProtectHome=read-only` makes every path under `/home` unwritable,
  so a Chromium pointed at a `--user-data-dir` there exits **21** about a second
  into every launch — and because `Restart=always` governs the AGENT, which is
  fine, `systemctl status` shows `active (running)` with `NRestarts=0` while the
  wall is black. Neither the unit nor the process table says why: the agent logs
  only the child's exit code, and `cage` and `chromium` both start correctly by
  hand, because a shell has no sandbox. Discriminate by toggling ONE directive
  in a `/run/systemd/system/<unit>.d/` drop-in and re-reading the agent's own
  `browserRestartCount`. Keep the profile in the unit's `StateDirectory`
  (`/var/lib/pikioskd`), never under `/home`; `tests/test_unit_sandbox.py` joins
  the unit's writable set to the settings default so the two cannot drift again.
  Measured on the first panel, 2026-09-10, on 1.0.0's first hardware install.
- **`NoNewPrivileges=yes` FORBIDS `sudo` ENTIRELY, AND THE FAILURE IS SILENT.**
  It sets `PR_SET_NO_NEW_PRIVS`, which stops a setuid binary elevating at all;
  sudo does not degrade, it refuses — `sudo: The "no new privileges" flag is
  set, which prevents sudo from running as root.` Measured with
  `prctl(PR_SET_NO_NEW_PRIVS)` against a real setuid sudo, 2026-09-10: without
  the flag the same call reaches authentication ("a password is required"), so
  the flag, not the sudoers file, is the discriminator. A no-password sudoers
  drop-in therefore grants nothing while it is on. On this agent it hit
  `rebootDevice`, whose privileged command runs in a DETACHED shell — so the
  API answered `{"rebooting": true}`, the reply was already sent, and the panel
  never rebooted. Prove the privilege with a cheap `sudo -n true` BEFORE
  promising the action, and never with the real command. Same shape as
  ProtectHome killing the browser: a sandbox directive forbidding the one thing
  the service exists to do.
- **`StartLimitIntervalSec` IN `[Service]` IS SILENTLY IGNORED** — it is a
  `[Unit]` key. systemd says so once, at start, in a line that scrolls past
  (`Unknown key 'StartLimitIntervalSec' in section [Service], ignoring`), and
  the default start limit stays in force. A unit can carry it in the wrong
  section for its whole life and never fail.
- **A CONFIG DIRECTORY THE DAEMON CANNOT WRITE FAILS ONLY ON RESTART.** The
  agent persists settings by an atomic replace — temp file beside the target,
  fsync, rename — so it needs write permission on the DIRECTORY, not on the
  file. Created `root:kiosk 0750` it is readable and unwritable, and the shape
  of the failure is the trap: the in-memory value changes, the API answers 200,
  a read-back returns the NEW value, and the write is lost at the next restart
  with only a journal traceback to say so. Nothing on the request path reports
  it. `install -d -o "$KIOSK_USER"`, and let the test that joins install.sh's
  modes to the unit's `User=` keep it that way.
- **AN IN-MEMORY READ-BACK IS NOT A READ-BACK.** This agent's contract is that
  every write reports the stored value, and it did — from memory. When the
  persist failed, the map was already updated, so `listSettings` answered with
  the new value, the coordinator polled it as correct, the panel behaved as if
  it had taken, and the change vanished at the next restart. The write must
  ROLL MEMORY BACK when `save()` raises, or all-or-nothing is a claim the code
  does not honour. Permissions were only the trigger here; a full SD card or an
  ext4 that has flipped read-only produces it identically, and this fleet
  carries a sensor for the latter because it happens. Verified as an
  unprivileged user against an unwritable directory, 2026-09-10 — as root the
  same test passes and proves nothing, because root bypasses the check.
- **`systemctl is-active` PROVES NOTHING ABOUT A BROWSER.** Under
  `Restart=always` a crash-looping Chromium reports `active` for ever. The
  restart COUNT is the signal, which is why the agent publishes its own.
- **SIGNAL THE PROCESS GROUP, NOT THE LEADER.** `cage` forks Chromium and
  Chromium forks a zygote and a renderer per tab; signalling the leader alone
  leaves orphans holding the DRM device, after which the next `cage` cannot take
  it and the wall stays black with no error anybody sees.
- **AN HDMI MONITOR HAS NO `/sys/class/backlight` DEVICE.** Brightness is
  genuinely unavailable there, and reporting a number nothing controls is worse
  than reporting none.
- **`XDG_RUNTIME_DIR` IS DEFERRED TO, NEVER OVERWRITTEN.** A value already in
  the environment was put there by whatever started the process and is
  authoritative. Overwriting it is how a fleet grows a per-host variant that
  looks like hardware and is not.
- **`iw` IS NOT INSTALLED** on Raspberry Pi OS images. `/proc/net/wireless` is
  always present when a driver is bound and needs no package and no privileges.
  `nmcli` reports a 0–100 quality PERCENTAGE, which is a different quantity from
  dBm and is derived from it.
- **NO POINTER DEVICE MEANS THE CURSOR NEVER MOVES AND NEVER LEAVES.**
  Chromium's Ozone/Wayland cursor path needs a `wl_pointer` enter serial to
  change or clear the cursor surface, and on a Pi with no mouse that serial
  never arrives — so whatever arrow was drawn at launch stays exactly where it
  is, for ever. `cage` has no cursor-hide option and there is no Chromium flag
  for it. `cursor: none` FROM INSIDE THE PAGE is what makes Chromium commit a
  null cursor surface, which is why the fix is an extension and not a switch.
  A CDP screenshot never contains the compositor cursor — it is
  `Page.captureScreenshot`, taken out of Chromium's RENDERER compositor, while
  the cursor is a surface handed to cage, so `image.<host>_screenshot` shows no
  cursor whether or not one is on the glass. A sweep of them once reported four
  walls clean with one of them stuck. What IS readable off-device is whether
  the hide-cursor rule reached the live document
  (`binary_sensor.<host>_cursor_hidden`, from
  `getComputedStyle(document.documentElement).cursor` over CDP). Necessary, not
  sufficient: ON with a cursor still on the wall is the compositor-surface
  fault, OFF is a fix that never arrived. Only `grim -c` on the host or eyes on
  the glass closes the last step.
- **`--load-extension` IS A BRANDED-CHROME QUESTION, NOT A VERSION QUESTION,
  AND THE VERSION NUMBER IS THE MISLEADING HALF.** Google-BRANDED Chrome
  restricted the switch at 137 and removed it — together with its
  `--disable-features=DisableLoadExtensionCommandLineSwitch` escape hatch — at
  142. Unbranded Chromium keeps both, and Raspberry Pi OS packages unbranded
  Chromium, which is what `chromiumBinary` defaults to. Measured on the live
  fleet 2026-09-11: all four panels report 152.0.7977.82. THAT IS THE VERSION
  AND NOT THE BEHAVIOUR — whether an extension actually loads on these hosts
  is unverified, and stays unverified until somebody reads a wall. Reading the
  version alone says "past 142, therefore broken" and is wrong here; carrying
  the workaround "just in case" writes a gate that does not govern this fleet
  into the flag set. On a branded build the switch is
  ignored SILENTLY — the browser starts, the wall paints, the extension is
  simply absent — so the agent logs the directory it loaded from.
- **CHROMIUM KEEPS ONE `--disable-features` AND DOES NOT SAY WHICH.** A second
  one appended anywhere discards the first, with no warning and no way to tell
  from the process table which survived. Compose the value from a list of
  feature names so adding one is an edit to a list, never a new flag.
- **`ANCHOR ON THE `URL=` LINE** when reading a start URL out of a legacy
  `kiosk.sh`, never on "the first URL in the file". These scripts document their
  own history in comments, so a pattern that can match prose eventually matches
  prose — and did, silently, for weeks.
- **`vcgencmd get_throttled`'s HIGH HALF IS STICKY.** Bits 16–19 are
  since-boot, so a wall that browned out at 3am still reports it at noon. An
  unreadable bitmask is *unknown*, never clean: a throttle report defaulting to
  healthy is exactly the reading a browning-out panel would give.
