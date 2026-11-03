#!/bin/sh
# Make an installer bundle of Relay, for a machine that has no clone of this repository:
# dist/relay-<version>.tar.gz with the code of a release (what git has at REF), its Python dependencies (pure
# Python: one bundle for every CPU) and install.sh. On the other machine, as the user that will run Relay:
#
#   tar xzf relay-<version>.tar.gz && sh relay-<version>/install.sh
#
#   sh scripts/make-bundle.sh            the newest release (git's newest tag; without tags, what's committed now)
#   sh scripts/make-bundle.sh v4.0.1     another one
#   sh scripts/make-bundle.sh HEAD       what's committed now
set -e

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
REF=${1:-$(git -C "$ROOT" describe --tags --abbrev=0 2>/dev/null || echo HEAD)}
OUT=${DIST:-$ROOT/dist}
VERSION=$(git -C "$ROOT" show "$REF:src/relay/__init__.py" | sed -n 's/^__version__ = "\(.*\)"$/\1/p')
[ -n "$VERSION" ] || { echo "make-bundle: no Relay version at $REF" >&2; exit 1; }
NAME=relay-$VERSION

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
# What git has, and only that: no one's own provider kinds, local notes or caches
git -C "$ROOT" archive --prefix="$NAME/" "$REF" | tar -x -C "$STAGE"
BUNDLE=$STAGE/$NAME
# Its Python dependencies, as scripts/vendor.sh installs them (this clone's, when they're for the same lock)
if [ -f "$ROOT/vendor/.requirements.lock" ] \
    && [ "$(cat "$BUNDLE/requirements.lock")" = "$(cat "$ROOT/vendor/.requirements.lock")" ]; then
    cp -R "$ROOT/vendor" "$BUNDLE/vendor"
else
    sh "$BUNDLE/scripts/vendor.sh" >/dev/null
fi
find "$BUNDLE/vendor" -name __pycache__ -type d -prune -exec rm -rf {} +
# (this clone's installer: releases from before it had one are bundled too)
cp "$ROOT/scripts/install-bundle.sh" "$BUNDLE/install.sh"
# Its files: an upgrade from the next bundle removes those that bundle no longer has (and nobody's own)
(cd "$BUNDLE" && find . \( -type f -o -type l \) | sed 's|^\./||' | LC_ALL=C sort > "$STAGE/files")
echo ".relay-bundle" | LC_ALL=C sort -m - "$STAGE/files" > "$BUNDLE/.relay-bundle"

mkdir -p "$OUT"
tar -C "$STAGE" -czf "$OUT/$NAME.tar.gz" "$NAME"
echo "$OUT/$NAME.tar.gz ($(du -h "$OUT/$NAME.tar.gz" | cut -f1)): Relay $VERSION from $REF"
echo "On the other machine: tar xzf $NAME.tar.gz && sh $NAME/install.sh"
