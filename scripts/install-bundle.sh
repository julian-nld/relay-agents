#!/bin/sh
# Install Relay from this bundle (scripts/make-bundle.sh makes it), or upgrade an install from an earlier one: the
# system packages it needs, the code in ~/relay, and the service, as scripts/install-relay.sh sets it up.
# Run it as the user that will run Relay; it asks for sudo when it needs it.
#
#   sh install.sh                  into ~/relay, as a user service (recommended: rootless podman runs as you)
#   sh install.sh --system         as a system service running as you
#   sh install.sh --port 8500      serve on another port
#   sh install.sh --dir /srv/relay somewhere else
#   sh install.sh --no-service     only the code (then: ./relay serve, to try it)
set -e

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DIR=$HOME/relay
MODE=--user
PORT=
SERVICE=yes
while [ $# -gt 0 ]; do
    case "$1" in
        --user) MODE=--user ;;
        --system) MODE=--system ;;
        --port) PORT="$2"; shift ;;
        --dir) DIR="$2"; shift ;;
        --no-service) SERVICE=no ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\n== %s\n' "$*"; }
die() { echo "install: $*" >&2; exit 1; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
version() { sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$1/src/relay/__init__.py" 2>/dev/null; }

[ -f "$HERE/.relay-bundle" ] || die "run the install.sh of a bundle (scripts/make-bundle.sh makes one)"
if [ "$SERVICE" = yes ] && [ "$(id -u)" -eq 0 ]; then
    die "run this as the user that will run Relay, not as root: it asks for sudo when it needs it"
fi
case "$DIR" in /*) ;; *) DIR=$(pwd -P)/$DIR ;; esac
NEW=$(version "$HERE")
if [ "$SERVICE" = yes ] && command -v systemctl >/dev/null 2>&1; then
    # One Relay service per user (or machine): one from another folder isn't taken over (systemd says which)
    if [ "$MODE" = --user ]; then scope=--user; else scope=; fi
    # shellcheck disable=SC2086
    other=$(systemctl $scope cat relay.service 2>/dev/null | sed -n 's|^ExecStart=.* \(/.*\)/relay serve.*|\1|p' | head -n 1)
    if [ -n "$other" ] && [ "$other" != "$DIR" ] && [ "$other" != "$(cd "$DIR" 2>/dev/null && pwd -P)" ]; then
        die "Relay in $other is this $( [ "$MODE" = --user ] && echo "user's" || echo "machine's") service already: \
upgrade that one (--dir $other), or remove its service first (sh $other/scripts/install-relay.sh --uninstall $MODE)"
    fi
fi

say "Checking the system packages"
need=
command -v podman >/dev/null 2>&1 || need="$need podman"
command -v git >/dev/null 2>&1 || need="$need git"
command -v python3 >/dev/null 2>&1 || need="$need python3"
if [ -n "$need" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing$need (apt)"
        as_root apt-get update -q
        # shellcheck disable=SC2086
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q $need
    elif command -v dnf >/dev/null 2>&1; then
        say "Installing$need (dnf)"
        # shellcheck disable=SC2086
        as_root dnf install -y -q $need
    else
        die "install$need with this system's package manager, then run this again"
    fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Relay needs Python 3.10 or newer; this has $(python3 -V 2>&1)"
echo "podman, git and Python $(python3 -c 'import platform; print(platform.python_version())') are there"

if [ "$HERE" = "$(cd "$DIR" 2>/dev/null && pwd -P)" ]; then
    say "Relay $NEW is in $DIR"  # (the bundle unpacked where it's to be)
elif [ -e "$DIR/.git" ]; then
    die "$DIR is a clone of Relay's repository: upgrade it with git pull there, or pick another folder (--dir)"
elif [ -f "$DIR/.relay-bundle" ]; then
    say "Upgrading Relay $(version "$DIR") to $NEW in $DIR"
    # The old bundle's files that this one doesn't have go; files no bundle had (your own) stay
    LC_ALL=C sort "$DIR/.relay-bundle" | LC_ALL=C comm -23 - "$HERE/.relay-bundle" | while IFS= read -r file; do
        rm -f "$DIR/$file"
    done
    (cd "$HERE" && tar -cf - .) | (cd "$DIR" && tar -xf -)
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR")" ]; then
    die "$DIR has files of its own that no Relay bundle put there: pick another folder (--dir)"
else
    say "Installing Relay $NEW in $DIR"
    mkdir -p "$DIR"
    (cd "$HERE" && tar -cf - .) | (cd "$DIR" && tar -xf -)
fi
"$DIR/relay" --version >/dev/null || die "Relay can't start from $DIR; see the message above"

# The default workspace root: where your projects go (Settings can add more)
mkdir -p "$HOME/projects"

if [ "$SERVICE" = yes ]; then
    # (it checks the machine with relay doctor, then installs and (re)starts the service)
    sh "$DIR/scripts/install-relay.sh" "$MODE" ${PORT:+--port "$PORT"}
else
    [ -z "$PORT" ] || "$DIR/relay" port "$PORT" >/dev/null
    say "Relay $NEW is in $DIR"
    echo "Try it: $DIR/relay serve    (or as a service: sh $DIR/scripts/install-relay.sh --user)"
fi

say "Next: the agents"
echo "Relay works with the agents installed for this user. Any of these will do, then Settings -> Providers:"
echo "  - opencode 2 (free Zen models, no account): curl -fsSL https://opencode.ai/v2/install | bash"
echo "  - Claude Code: install it as Anthropic describes, then run 'claude' once to sign in"
echo "  - GitHub Copilot: copilot login"
echo "Upgrading later: a newer bundle's install.sh, with the same options. Your data stays in ~/.local/share/relay."
