"""`relay backup`: all of Relay's data in one archive, while Relay runs.

The database is copied with SQLite's backup API (a consistent copy, whatever runs
write meanwhile); everything else in the data folder (settings, providers, chats,
task worktrees, checkpoints, uploads, opencode-manager's profiles with their logins)
goes in as it is; and each service's data, which lives in a podman volume, as
`volumes/<volume>.tar` (its box paused while it's copied: relay.production.export_data).
To restore: stop Relay, extract the archive into an empty data folder (`tar -xzf FILE
-C ~/.local/share/relay`), start Relay: a service whose data volume isn't there gets it
back from `volumes/` when its box is made.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from relay import config
from relay.production import export_data

DATABASE = "relay.db"
SKIPPED = {DATABASE, f"{DATABASE}-wal", f"{DATABASE}-shm", f"{DATABASE}-journal"}


def default_file() -> Path:
    return Path.cwd() / time.strftime("relay-backup-%Y%m%d-%H%M%S.tar.gz")


def write(target: Path) -> tuple[int, list[str]]:
    """Write the archive; (files in it, files that couldn't be read)."""
    data = config.data_dir()
    if not (data / DATABASE).is_file():
        raise FileNotFoundError(f"There's no Relay data in {data}")
    target = target.resolve()
    skipped: list[str] = []
    count = 0
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / DATABASE
        source = sqlite3.connect(data / DATABASE)
        try:
            with sqlite3.connect(copy) as destination:
                source.backup(destination)
        finally:
            source.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.partial")
        try:
            # It holds logins and API keys: only for its owner, from the start.
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as archive:
                archive.add(copy, arcname=DATABASE)
                count += 1
                for top, dirs, files in os.walk(data):
                    here = Path(top)
                    dirs.sort()
                    for name in sorted(files):
                        path = here / name
                        relative = path.relative_to(data)
                        if str(relative) in SKIPPED or path.resolve() in (target, partial.resolve()):
                            continue
                        if _add(archive, path, str(relative)):
                            count += 1
                        else:
                            skipped.append(str(relative))
                for service_id, name, box in _services(copy):  # (their data: in podman's volumes, not the folder)
                    held = Path(tmp) / f"{name}.tar"
                    with _copying(data / DATABASE, service_id):
                        why = export_data(name, box, held, _podman)
                    if why is None and _add(archive, held, f"volumes/{name}.tar"):
                        count += 1
                    else:
                        skipped.append(f"volumes/{name}.tar ({why or 'unreadable'})")
                    held.unlink(missing_ok=True)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
    return count, skipped


def _services(database: Path) -> list[tuple[str, str | None]]:
    """Each service's data volume, and its live box (paused while the volume is copied)."""
    with sqlite3.connect(database) as db:
        try:
            rows = db.execute("SELECT s.id, v.number FROM services s LEFT JOIN service_versions v "
                              "ON v.id = s.live_version AND s.stopped IS NULL").fetchall()
        except sqlite3.OperationalError:  # (a database from before services)
            return []
    return [(service_id, f"relay-data-{service_id[:12]}", f"relay-prod-{service_id[:8]}-{number}" if number else None)
            for service_id, number in rows]


@contextlib.contextmanager
def _copying(database: Path, service_id: str):
    """An operation on the service while its data is copied (its box paused): the running Relay doesn't
    health-check it meanwhile, so a pause isn't taken for a failure. One already going: left as it is."""
    with sqlite3.connect(database, timeout=30) as db:
        marked = db.execute("UPDATE services SET operation = 'backup', operation_at = ? WHERE id = ? AND "
                            "operation IS NULL", (time.time(), service_id)).rowcount
    try:
        yield
    finally:
        if marked:
            with sqlite3.connect(database, timeout=30) as db:
                db.execute("UPDATE services SET operation = NULL, operation_at = NULL WHERE id = ? AND "
                           "operation = 'backup'", (service_id,))


def _podman(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["podman", *args], 1, "", f"podman: {exc}")


def _add(archive: tarfile.TarFile, path: Path, name: str) -> bool:
    """Add a file as it is at one moment: read whole first, so one that changes meanwhile (a
    run writing) can't leave a member shorter than its header says. False when it can't be read."""
    try:
        if path.is_symlink() or not path.is_file():
            # A link, a named pipe, …: as what it is, without reading it (a pipe would wait forever).
            info = archive.gettarinfo(str(path), arcname=name)
            if info is None:  # a socket: nothing to keep
                return True
            info.size = 0
            archive.addfile(info)
            return True
        with open(path, "rb") as source, tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as held:
            info = archive.gettarinfo(fileobj=source, arcname=name)
            # Its contents every time, also for a second name of the same file (no hard link entry).
            info.type, info.linkname = tarfile.REGTYPE, ""
            shutil.copyfileobj(source, held)
            info.size = held.tell()
            held.seek(0)
            archive.addfile(info, held)
        return True
    except OSError:  # e.g. a file a container made that only it can read
        return False


def main(file: str | None) -> int:
    target = Path(file).expanduser() if file else default_file()
    try:
        count, skipped = write(target)
    except (OSError, sqlite3.Error) as exc:
        print(f"relay backup: {exc}", file=sys.stderr)
        return 1
    size = target.stat().st_size / 1e6
    print(f"Wrote {target} ({count} files, {size:.1f} MB).")
    if skipped:
        print(f"{len(skipped)} file(s) couldn't be read and are missing, e.g. {skipped[0]}", file=sys.stderr)
    print(f"Restore: stop Relay, then tar -xzf {target.name} -C {config.data_dir()} (an empty folder).")
    return 0
