#!/bin/sh
# Install Relay as a systemd service, so it starts at boot and restarts after
# a crash (e.g. on a Raspberry Pi).
#
#   sh scripts/install-relay.sh              system service (uses sudo)
#   sh scripts/install-relay.sh --user       user service, no root needed
#   sh scripts/install-relay.sh --port 8500  serve on another port
#   sh scripts/install-relay.sh --uninstall  stop and remove the service
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
MODE=system
PORT=
ACTION=install
while [ $# -gt 0 ]; do
    case "$1" in
        --user) MODE=user ;;
        --system) MODE=system ;;
        --port) PORT="$2"; shift ;;
        --uninstall) ACTION=uninstall ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install-relay: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\n== %s\n' "$*"; }
die() { echo "install-relay: $*" >&2; exit 1; }

command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) is required"

if [ "$MODE" = user ]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    ctl() { systemctl --user "$@"; }
    as_root() { "$@"; }
else
    UNIT_DIR=/etc/systemd/system
    ctl() { as_root systemctl "$@"; }
    if [ "$(id -u)" -eq 0 ]; then
        as_root() { "$@"; }
    else
        command -v sudo >/dev/null 2>&1 || die "sudo is needed for a system service (or use --user)"
        as_root() { sudo "$@"; }
    fi
fi
UNIT="$UNIT_DIR/relay.service"

if [ "$ACTION" = uninstall ]; then
    say "Removing the Relay service ($MODE)"
    ctl disable --now relay.service 2>/dev/null || true
    as_root rm -f "$UNIT"
    ctl daemon-reload
    echo "Removed. Relay's data (RELAY_HOME, default ~/.local/share/relay), its containers and images are untouched."
    echo "To remove those too: docs/GUIDE.md, Removing Relay."
    exit 0
fi

say "Checking Python"
"$ROOT/relay" --version >/dev/null 2>&1 || {
    if [ ! -d "$ROOT/vendor" ]; then
        say "Installing Python dependencies into vendor/"
        sh "$ROOT/scripts/vendor.sh"
    fi
    "$ROOT/relay" --version >/dev/null || die "Relay can't start; see the message above"
}
echo "Relay $("$ROOT/relay" --version)"
if [ -n "$PORT" ]; then
    # (in Relay's settings, so relay doctor, relay provider-check and the addresses below know it too)
    echo "Serving on port $("$ROOT/relay" port "$PORT")"
fi

say "Checking this machine"
"$ROOT/relay" doctor || die "fix the problems above and run this again"

say "Installing the $MODE service"
TMP=$(mktemp)
if [ "$MODE" = user ]; then
    "$ROOT/relay" systemd-unit --user > "$TMP"
else
    "$ROOT/relay" systemd-unit > "$TMP"
fi
as_root mkdir -p "$UNIT_DIR"
as_root install -m 644 "$TMP" "$UNIT"
rm -f "$TMP"
echo "Wrote $UNIT"
ctl daemon-reload
ctl enable relay.service >/dev/null 2>&1
ctl restart relay.service

# Lingering: a user service runs without anyone logged in, and the user's runtime folder (which
# rootless podman, the agents' containers, needs) exists from boot for a system service too.
if loginctl enable-linger "$(id -un)" 2>/dev/null || sudo loginctl enable-linger "$(id -un)" 2>/dev/null; then
    echo "Lingering is on: the service and its containers run after a reboot, with nobody logged in."
else
    echo "Note: run 'sudo loginctl enable-linger $(id -un)' so Relay's containers work right after a reboot."
fi

if [ "$MODE" = user ]; then JOURNAL="journalctl --user -u relay"; else JOURNAL="journalctl -u relay"; fi

say "Waiting for Relay to answer"
PORT=$("$ROOT/relay" port)
answers() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1
    else
        ctl is-active --quiet relay.service  # no curl: trust systemd
    fi
}
i=0
until answers; do
    i=$((i + 1))
    if [ $i -ge 30 ]; then
        ctl status relay.service --no-pager || true
        die "Relay didn't start; see the status above and the logs: $JOURNAL"
    fi
    sleep 1
done

echo "Relay is running and starts automatically from now on:"
"$ROOT/relay" urls --port "$PORT" | sed 's/^/  /'
echo "Open it and make your account: the first visitor gets it."
echo "The first time, it builds its containers' images in the background: a few minutes. The page says so meanwhile."
echo "Logs: $JOURNAL -f"
echo "Remove: sh scripts/install-relay.sh --uninstall$( [ "$MODE" = user ] && echo ' --user')"
