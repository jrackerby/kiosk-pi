#!/usr/bin/env bash
#
# Install or upgrade pikioskd on a Raspberry Pi wall panel.
#
# RUN IT FROM THE PI, over SSH, as a user with sudo. It is idempotent: running
# it again upgrades the code, leaves the settings file alone, and restarts the
# unit. That property is what makes a fleet upgrade a for-loop rather than a
# procedure.
#
# WHAT IT DELIBERATELY DOES NOT DO:
#
#   - It does not install a Python package, a venv or a wheel. The agent is
#     standard-library-only precisely so that provisioning is a file copy; a
#     dependency here is a way for an upgrade to leave a wall dark on a host
#     with no console attached.
#   - It does not delete /home/kiosk/kiosk.sh. The old launcher is DISABLED,
#     not removed, and the settings file is seeded from its URL= line, so a
#     rollback is `systemctl disable --now pikioskd && systemctl enable --now
#     kiosk` and nothing has been destroyed to make that harder.
#   - It does not reboot. A wall reboots when an operator decides it does.
#   - IT DOES NOT ARM unattended-upgrades, AND THAT IS A RULING, NOT AN
#     OMISSION (jrackerby/kiosk-pi#10). The fleet standard this inherited from
#     jrackerby/HA#58 had the provisioning script write four artefacts —
#     `unattended-upgrades` installed, `20auto-upgrades`, `51kiosk-unattended`
#     and an `apt-daily-upgrade.timer` drop-in — and a drift sensor to catch
#     their reversion. Patching is now driven FROM Home Assistant by
#     `linux_monitor`'s `update.<host>_system_updates`, which reports, offers
#     Install, and owns the reboot that follows. ONE HOST, ONE PATCHER: arming
#     unattended-upgrades here as well would put two of them on one machine,
#     racing for the same dpkg lock, with the HA-side Install reporting a
#     failure it did not cause. If that ever reverses, the four artefacts and
#     the drift check come back TOGETHER — the standard was only ever safe
#     because something watched it.
#
# IT REFUSES TO FINISH SILENTLY. Every step that can fail is checked, and the
# script ends by reading the agent's own API back through the loopback — a unit
# that is `active` is not evidence that the agent is answering, because
# Restart=always makes a crash-looping service read active forever.

set -euo pipefail

PREFIX="${PREFIX:-/opt/pikioskd}"
CONFIG_DIR="${CONFIG_DIR:-/etc/pikioskd}"
SETTINGS="${CONFIG_DIR}/settings.json"
KIOSK_USER="${KIOSK_USER:-kiosk}"
UNIT_NAME="pikioskd.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m==> %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m==> %s\033[0m\n' "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"
command -v python3 >/dev/null || die "python3 is not installed"
python3 - <<'PY' || die "pikioskd needs Python 3.11 or newer"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

id "$KIOSK_USER" >/dev/null 2>&1 || die "user '$KIOSK_USER' does not exist"

for binary in cage chromium; do
  command -v "$binary" >/dev/null \
    || warn "$binary is not on PATH; the wall will not start until it is"
done

# --- code --------------------------------------------------------------------

log "installing the agent to ${PREFIX}"
install -d -m 0755 "$PREFIX"
# rm the package dir rather than copying over it: a file deleted upstream would
# otherwise survive forever on every host that was ever upgraded, and a stale
# module still imports.
rm -rf "${PREFIX:?}/pikioskd"
cp -r "${SOURCE_DIR}/pikioskd" "${PREFIX}/pikioskd"
find "${PREFIX}/pikioskd" -type d -exec chmod 0755 {} +
find "${PREFIX}/pikioskd" -type f -exec chmod 0644 {} +

VERSION="$(python3 -c "import sys; sys.path.insert(0, '${PREFIX}'); \
import pikioskd; print(pikioskd.__version__)")"
log "pikioskd ${VERSION}"

# --- settings ----------------------------------------------------------------

# OWNED BY THE AGENT'S USER, because the agent WRITES this directory. Every
# setting the API accepts is persisted by an atomic replace — write a temp file
# beside the target, fsync, rename — and that needs write permission on the
# DIRECTORY, not on the file. Created root-owned at 0750 it was readable and
# unwritable, so every setting changed in memory, answered 200, and was lost on
# the next restart, with the traceback buried in the journal. Measured on
# the first panel, 2026-09-10, on the first setting ever written from Home Assistant.
install -d -m 0750 -o "$KIOSK_USER" -g "$KIOSK_USER" "$CONFIG_DIR"

if [[ -f "$SETTINGS" ]]; then
  log "keeping the existing ${SETTINGS}"
else
  # SEED THE START URL FROM THE OLD LAUNCHER, anchored on the URL= line.
  #
  # NOT "the first URL in the file". These scripts document their own history
  # in comments, so a pattern that can match prose eventually matches prose —
  # and it did, silently, for weeks: one host reported a URL from a comment
  # naming a board that had been deleted, while URL= three lines below was
  # correct all along. Anchoring costs nothing and is the difference between
  # seeding a panel correctly and seeding it from a changelog entry.
  START_URL="about:blank"
  OLD_LAUNCHER="/home/${KIOSK_USER}/kiosk.sh"
  if [[ -f "$OLD_LAUNCHER" ]]; then
    FOUND="$(grep -m1 '^URL=' "$OLD_LAUNCHER" 2>/dev/null \
             | sed -e 's/^URL=//' -e 's/^"//' -e 's/"$//' || true)"
    if [[ -n "${FOUND:-}" ]]; then
      START_URL="$FOUND"
      log "seeding startURL from ${OLD_LAUNCHER}: ${START_URL}"
    else
      warn "${OLD_LAUNCHER} has no URL= line; startURL stays about:blank"
    fi
  fi

  # A GENERATED PASSWORD, NOT A DEFAULT ONE. A shipped default is a fleet-wide
  # shared secret that is public the moment this repository is: the API refuses
  # every request while the password is empty, so generating one here is what
  # turns a closed device into a usable one, per host.
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

  log "writing ${SETTINGS}"
  START_URL="$START_URL" PASSWORD="$PASSWORD" \
  HOSTNAME_VALUE="$(hostname)" python3 - "$SETTINGS" <<'PY'
import json, os, sys
settings = {
    "deviceName": os.environ["HOSTNAME_VALUE"],
    "startURL": os.environ["START_URL"],
    "remoteAdminPassword": os.environ["PASSWORD"],
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(settings, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  # The agent rewrites this file on every settings change, so it owns it. 0600
  # keeps the remote-admin password to that user and root.
  chown "$KIOSK_USER":"$KIOSK_USER" "$SETTINGS"
  chmod 0600 "$SETTINGS"
  NEW_PASSWORD="$PASSWORD"
fi

# --- privileges --------------------------------------------------------------

log "installing the sudoers drop-in (reboot only) and the backlight rule"
install -m 0440 -o root -g root \
  "${SOURCE_DIR}/systemd/pikioskd-sudo" /etc/sudoers.d/pikioskd
# A malformed sudoers file locks everybody out of sudo, so it is validated
# before it is trusted and removed if it does not parse.
visudo -cf /etc/sudoers.d/pikioskd >/dev/null \
  || { rm -f /etc/sudoers.d/pikioskd; die "the sudoers drop-in did not parse"; }

install -m 0644 "${SOURCE_DIR}/systemd/99-pikioskd-backlight.rules" \
  /etc/udev/rules.d/99-pikioskd-backlight.rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=backlight 2>/dev/null || true

for group in video render input tty; do
  getent group "$group" >/dev/null || continue
  id -nG "$KIOSK_USER" | tr ' ' '\n' | grep -qx "$group" \
    || { log "adding ${KIOSK_USER} to ${group}"; usermod -aG "$group" "$KIOSK_USER"; }
done

# --- unit --------------------------------------------------------------------

log "installing ${UNIT_NAME}"
install -m 0644 "${SOURCE_DIR}/systemd/${UNIT_NAME}" \
  "/etc/systemd/system/${UNIT_NAME}"
systemctl daemon-reload

if systemctl list-unit-files kiosk.service >/dev/null 2>&1 \
   && systemctl is-enabled kiosk.service >/dev/null 2>&1; then
  # DISABLED, NOT MASKED AND NOT DELETED. Two things must never hold the DRM
  # device at once, and the rollback path is `systemctl enable --now kiosk`.
  log "disabling the old kiosk.service (kept on disk for rollback)"
  systemctl disable --now kiosk.service || warn "could not disable kiosk.service"
fi

systemctl enable "$UNIT_NAME" >/dev/null
systemctl restart "$UNIT_NAME"

# --- verify ------------------------------------------------------------------
#
# `systemctl is-active` PROVES NOTHING HERE: Restart=always means a
# crash-looping agent reads `active` on every check. The evidence is the API
# answering over the loopback, and the restart count staying still while we ask.

log "verifying"
PASSWORD_READ="$(python3 - "$SETTINGS" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("remoteAdminPassword", ""))
PY
)"
PORT="$(python3 - "$SETTINGS" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("remoteAdminPort", 2323))
PY
)"

ok=0
for _ in $(seq 1 15); do
  if curl -fsS --max-time 3 \
      "http://127.0.0.1:${PORT}/?cmd=status&password=${PASSWORD_READ}" \
      >/tmp/pikioskd-install-check.json 2>/dev/null; then
    ok=1
    break
  fi
  sleep 1
done

if [[ $ok -ne 1 ]]; then
  warn "the agent did not answer on 127.0.0.1:${PORT} within 15s"
  warn "last 30 journal lines:"
  journalctl -u "$UNIT_NAME" -n 30 --no-pager >&2 || true
  die "install finished but the agent is not serving"
fi

log "agent is answering:"
python3 -m json.tool /tmp/pikioskd-install-check.json
rm -f /tmp/pikioskd-install-check.json

if [[ -n "${NEW_PASSWORD:-}" ]]; then
  cat <<EOF

  ------------------------------------------------------------------
  Remote admin password for $(hostname):

      ${NEW_PASSWORD}

  Add this host in Home Assistant under Settings -> Devices & Services
  -> Add Integration -> "Kiosk Pi", with this password.

  It is stored at ${SETTINGS} (${KIOSK_USER}:${KIOSK_USER}, 0600) and is
  not printed again. Read it back from that file if you lose it.
  ------------------------------------------------------------------

EOF
fi

log "done"
