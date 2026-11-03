#!/bin/sh
# Install the Python dependencies (for Relay and opencode-manager) into vendor/,
# so nothing needs to be pip-installed. They are pure Python, so one vendor/
# works on every OS and CPU architecture. The launchers run this again by
# themselves when requirements.lock changed (an upgrade).
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
PY=${PYTHON:-python3}

if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "vendor.sh: pip is needed: sudo apt install python3-pip (Debian, Ubuntu), sudo dnf install python3-pip (Fedora)" >&2
    exit 1
fi
# One at a time (two launchers starting after an upgrade): the second waits, then does it again.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$ROOT/.vendor.lock"
    flock 9
fi
# Into a new folder first: a failed install (no internet) leaves the old one as it was.
rm -rf "$ROOT/vendor.new"
"$PY" -m pip install -q --target "$ROOT/vendor.new" --no-compile --no-deps --disable-pip-version-check \
    -r "$ROOT/requirements.lock"
rm -rf "$ROOT/vendor.new/bin"
cp "$ROOT/requirements.lock" "$ROOT/vendor.new/.requirements.lock"
rm -rf "$ROOT/vendor.old"
[ ! -d "$ROOT/vendor" ] || mv "$ROOT/vendor" "$ROOT/vendor.old"
mv "$ROOT/vendor.new" "$ROOT/vendor"
rm -rf "$ROOT/vendor.old"
echo "Dependencies installed in $ROOT/vendor"
