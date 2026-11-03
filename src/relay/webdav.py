"""Network drives: a project's folder over WebDAV, to mount on your own computer.

To try what an agent wrote, open its folder as a drive: Windows (Map
network drive), macOS (Finder → Connect to Server), Linux (Files, gio or
davfs2) all speak WebDAV. Each share is one folder under a random address,
/dav/<token>/, which is the only key (Relay runs on your own network); it
can be read-only. What you change there is committed before the agent's
next turn, like any change made outside Relay.

A small server: what those clients need (RFC 4918 class 1 and 2, with
locks that are granted but not enforced). The shared folder's own `.git`
(its repository: Relay's checkpoints and history) stays out of sight; a
repository inside it (a project copied in, with its own `.git`) is a folder
like any other. The files desktops leave everywhere (macOS's `.DS_Store` and `._*`,
Windows's `Thumbs.db` and `desktop.ini`) are accepted but not kept, so they
don't end up in the project, and copying doesn't fail over them.
"""

from __future__ import annotations

import asyncio
import email.utils
import mimetypes
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from relay.db import Database

METHODS = ["OPTIONS", "PROPFIND", "PROPPATCH", "GET", "HEAD", "PUT", "DELETE", "MKCOL", "COPY", "MOVE",
           "LOCK", "UNLOCK"]
DAV_HEADERS = {"DAV": "1, 2", "MS-Author-Via": "DAV", "Allow": ", ".join(METHODS)}
HIDDEN = {".git"}  # in the shared folder itself (_hidden)
LOG_SIZE = 1_000_000  # bytes of relay-webdav.log kept: its end
REFUSED_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")
LOCK_SECONDS = 3600


class ShareError(Exception):
    pass


# Shares


def create(db: Database, user_id: int, directory: Path, name: str, read_only: bool | None = None) -> dict[str, Any]:
    """Share a folder (or return its share, updated; read_only None leaves that as it is)."""
    existing = db.one("SELECT * FROM shares WHERE user_id = ? AND directory = ?", (user_id, str(directory)))
    if existing:
        db.execute("UPDATE shares SET read_only = ?, name = ? WHERE token = ?",
                   (existing["read_only"] if read_only is None else 1 if read_only else 0, name, existing["token"]))
        return get(db, existing["token"])
    token = secrets.token_urlsafe(18)
    db.execute("INSERT INTO shares (token, user_id, directory, name, read_only, created) VALUES (?, ?, ?, ?, ?, ?)",
               (token, user_id, str(directory), name[:120], 1 if read_only else 0, time.time()))
    return get(db, token)


def get(db: Database, token: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM shares WHERE token = ?", (token,))
    return {**row, "read_only": bool(row["read_only"])} if row else None


def for_user(db: Database, user_id: int) -> list[dict[str, Any]]:
    return [{**r, "read_only": bool(r["read_only"])}
            for r in db.all("SELECT * FROM shares WHERE user_id = ? ORDER BY created DESC", (user_id,))]


def remove(db: Database, user_id: int, token: str) -> None:
    db.execute("DELETE FROM shares WHERE user_id = ? AND token = ?", (user_id, token))


# The protocol


async def root(request: Request) -> Response:
    """Windows asks the server's root (and /dav/) what it speaks before it maps a drive."""
    if request.method == "OPTIONS":
        return Response(status_code=200, headers=DAV_HEADERS)
    return _multistatus([])


async def handle(request: Request) -> Response:
    db: Database = request.app.state.relay.db
    share = get(db, request.path_params["token"])
    if share is None:
        return Response("No such network drive (any more)", status_code=404)
    base = Path(share["directory"])
    if not base.is_dir():
        return Response("The shared folder is gone", status_code=404)
    relative = request.path_params.get("path", "")
    if request.method == "OPTIONS":
        return Response(status_code=200, headers=DAV_HEADERS)
    try:
        target = _resolve(base, relative)
    except ShareError as exc:
        return _logged(request, Response(str(exc), status_code=403))
    except OSError as exc:  # (a name too long for a file, say: what an editor proposes from a pasted text)
        looking = request.method in ("PROPFIND", "GET", "HEAD")
        what = "No such file" if looking else "Couldn't do that"
        return _logged(request, Response(f"{what}: {exc.strerror or exc}", status_code=404 if looking else 400))
    prefix = f"/dav/{share['token']}/"
    method = request.method
    writes = method in ("PUT", "DELETE", "MKCOL", "MOVE", "COPY") or (method == "LOCK" and not target.exists())
    if writes and share["read_only"]:
        return _logged(request, Response("This network drive is read-only", status_code=403))
    handler = {
        "PROPFIND": _propfind, "PROPPATCH": _proppatch, "GET": _get, "HEAD": _get, "PUT": _put,
        "DELETE": _delete, "MKCOL": _mkcol, "COPY": _copy_or_move, "MOVE": _copy_or_move,
        "LOCK": _lock, "UNLOCK": _unlock,
    }.get(method)
    if handler is None:
        return _logged(request, Response(status_code=405, headers={"Allow": ", ".join(METHODS)}))
    try:
        response = await handler(request, base, target, prefix)
    except OSError as exc:  # e.g. a file the agent's container made as another user
        response = Response(f"Couldn't do that: {exc.strerror or exc}", status_code=403)
    return _logged(request, response)


def _logged(request: Request, response: Response) -> Response:
    """What clients asked and got, for when one says no (~/.cache/opencode-manager/relay-webdav.log, its last
    LOG_SIZE bytes)."""
    if request.method in ("PROPFIND", "GET", "HEAD") and (response.status_code < 400 or response.status_code == 404):
        return response  # just looking (an editor looks for its .vscode and the like at every turn)
    try:
        from opencode_manager import paths
        path = paths.host_cache_dir() / "relay-webdav.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_SIZE:
            kept = path.read_bytes()[-LOG_SIZE // 2:]
            path.write_bytes(kept[kept.find(b"\n") + 1:])
        with open(path, "a") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{request.method}\t{response.status_code}\t"
                      f"{unquote(request.url.path)}\t{request.headers.get('user-agent', '')[:80]}\n")
    except OSError:
        pass
    return response


def _resolve(base: Path, relative: str) -> Path:
    """A shared path (already decoded: a file may be called a%20b.txt) inside `base`."""
    parts = [p for p in relative.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ShareError("Outside the shared folder")
    if _hidden(parts):
        raise ShareError("Not shared")
    target = base.joinpath(*parts)
    # Symlinks may point anywhere inside the project, not out of it.
    probe = (target if target.exists() else target.parent).resolve()
    root = base.resolve()
    if probe != root and root not in probe.parents:
        raise ShareError("Outside the shared folder")
    if _hidden(probe.relative_to(root).parts):
        raise ShareError("Not shared")  # e.g. a symlink to .git
    return target


def _hidden(parts: Any) -> bool:
    """Whether a path in the share (its parts) is in the shared folder's own `.git`. One deeper is another
    repository's, copied in, say: a user's Dolphin couldn't copy a vault with its `.git` (a folder it may not make)."""
    return bool(parts) and parts[0] in HIDDEN


def _href(prefix: str, base: Path, path: Path) -> str:
    relative = path.relative_to(base).as_posix() if path != base else ""
    href = prefix + quote(relative)
    return href + "/" if path.is_dir() and not href.endswith("/") else href


def _props(path: Path, href: str) -> str:
    stat = path.stat()
    modified = email.utils.formatdate(stat.st_mtime, usegmt=True)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_ctime))
    name = escape(path.name)
    if path.is_dir():
        kind = "<D:resourcetype><D:collection/></D:resourcetype>"
        size = ""
        content_type = "<D:getcontenttype>httpd/unix-directory</D:getcontenttype>"
    else:
        kind = "<D:resourcetype/>"
        size = f"<D:getcontentlength>{stat.st_size}</D:getcontentlength>"
        guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content_type = f"<D:getcontenttype>{escape(guessed)}</D:getcontenttype>"
    return (f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>"
            f"<D:displayname>{name}</D:displayname>{kind}{size}{content_type}"
            f"<D:getlastmodified>{modified}</D:getlastmodified><D:creationdate>{created}</D:creationdate>"
            f"<D:getetag>{_etag(stat)}</D:getetag>"
            "<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>"
            "<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _etag(stat: os.stat_result) -> str:
    return f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'


def _multistatus(responses: list[str]) -> Response:
    body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
            + "".join(responses) + "</D:multistatus>")
    return Response(body, status_code=207, media_type="application/xml; charset=utf-8")


async def _propfind(request: Request, base: Path, target: Path, prefix: str) -> Response:
    if not target.exists():
        return Response(status_code=404)
    depth = request.headers.get("depth", "1")

    def collect() -> list[str]:
        found = [_props(target, _href(prefix, base, target))]
        if target.is_dir() and depth != "0":
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if target == base and child.name in HIDDEN:
                    continue
                try:
                    found.append(_props(child, _href(prefix, base, child)))
                except OSError:
                    continue  # e.g. a broken symlink
        return found

    return _multistatus(await asyncio.to_thread(collect))


async def _proppatch(request: Request, base: Path, target: Path, prefix: str) -> Response:
    """Windows sets file times and attributes this way (right after making a folder or a file):
    say yes to each property it names, as it checks them one by one, and keep the file's own."""
    if not target.exists():
        return Response(status_code=404)
    names = []
    try:
        root = ElementTree.fromstring(await request.body() or b"<x/>")
        for prop in root.iter("{DAV:}prop"):
            names += [child.tag for child in prop]
    except ElementTree.ParseError:
        pass
    props, spaces = "", {}
    for tag in names:
        space, _, local = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
        if space:
            alias = spaces.setdefault(space, f"ns{len(spaces)}")
            props += f'<{alias}:{local} xmlns:{alias}="{escape(space)}"/>'
        else:
            props += f"<{local}/>"
    return _multistatus([f"<D:response><D:href>{escape(_href(prefix, base, target))}</D:href><D:propstat>"
                         f"<D:prop>{props}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"])


async def _get(request: Request, base: Path, target: Path, prefix: str) -> Response:
    if target.is_dir():
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir()
                       if target != base or p.name not in HIDDEN)
        links = "".join(f'<li><a href="{escape(quote(n))}">{escape(n)}</a></li>' for n in names)
        return Response(f"<!doctype html><title>{escape(target.name)}</title><ul>{links}</ul>",
                        media_type="text/html; charset=utf-8")
    if not target.is_file():
        return Response(status_code=404)
    return FileResponse(target, headers={"ETag": _etag(target.stat())})


def _refused(target: Path) -> bool:
    return target.name in REFUSED_NAMES or target.name.startswith("._")


async def _put(request: Request, base: Path, target: Path, prefix: str) -> Response:
    if _refused(target):
        async for _ in request.stream():
            pass  # accepted, not kept
        return Response(status_code=201)
    if target.is_dir():
        return Response(status_code=405)
    if not target.parent.is_dir():
        return Response(status_code=409)
    existed = target.exists()
    temporary = target.with_name(f".{target.name}.relay-{uuid.uuid4().hex[:8]}")
    try:
        with open(temporary, "wb") as out:
            async for chunk in request.stream():
                await asyncio.to_thread(out.write, chunk)
        if existed:
            shutil.copymode(target, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return Response(status_code=204 if existed else 201, headers={"ETag": _etag(target.stat())})


async def _delete(request: Request, base: Path, target: Path, prefix: str) -> Response:
    if target == base:
        return Response("The shared folder itself stays", status_code=403)
    if not target.exists() and not target.is_symlink():
        return Response(status_code=404)
    if target.is_dir() and not target.is_symlink():
        await asyncio.to_thread(shutil.rmtree, target)
    else:
        target.unlink()
    return Response(status_code=204)


async def _mkcol(request: Request, base: Path, target: Path, prefix: str) -> Response:
    if target.exists():
        return Response(status_code=405)
    if not target.parent.is_dir():
        return Response(status_code=409)
    target.mkdir()
    return Response(status_code=201)


async def _copy_or_move(request: Request, base: Path, target: Path, prefix: str) -> Response:
    if not target.exists():
        return Response(status_code=404)
    destination = urlsplit(request.headers.get("destination", "")).path
    if not destination.startswith(prefix.rstrip("/")):
        return Response("Only within the same network drive", status_code=502)
    try:
        goal = _resolve(base, unquote(destination[len(prefix):]) if destination.startswith(prefix) else "")
    except ShareError as exc:
        return Response(str(exc), status_code=403)
    if goal == base or goal == target or target in goal.parents or goal in target.parents:
        # (onto a folder it's in would delete that folder, and so itself, first)
        return Response("Not into itself, nor onto a folder it's in", status_code=403)
    if _refused(goal):
        return Response(status_code=201)  # not kept, as with PUT
    if not goal.parent.is_dir():
        return Response(status_code=409)
    existed = goal.exists()
    if existed:
        if request.headers.get("overwrite", "T").upper() == "F":
            return Response(status_code=412)
        await asyncio.to_thread(shutil.rmtree if goal.is_dir() else os.unlink, goal)
    if request.method == "MOVE":
        await asyncio.to_thread(shutil.move, str(target), str(goal))
    elif target.is_dir():
        await asyncio.to_thread(shutil.copytree, target, goal, symlinks=True)
    else:
        await asyncio.to_thread(shutil.copy2, target, goal)
    return Response(status_code=204 if existed else 201)


async def _lock(request: Request, base: Path, target: Path, prefix: str) -> Response:
    """Granted, not enforced: Finder and Windows only write after locking.
    A lock on a name that doesn't exist yet makes an empty file, as RFC 4918 says."""
    await request.body()
    created = False
    if not target.exists() and not _refused(target):
        if not target.parent.is_dir():
            return Response(status_code=409)
        target.touch()
        created = True
    token = request.headers.get("if", "").strip("()<> ") or f"opaquelocktoken:{uuid.uuid4()}"
    body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
            "<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope>"
            f"<D:depth>{escape(request.headers.get('depth', 'infinity'))}</D:depth>"
            f"<D:timeout>Second-{LOCK_SECONDS}</D:timeout>"
            f"<D:locktoken><D:href>{escape(token)}</D:href></D:locktoken>"
            f"<D:lockroot><D:href>{escape(_href(prefix, base, target))}</D:href></D:lockroot>"
            "</D:activelock></D:lockdiscovery></D:prop>")
    return Response(body, status_code=201 if created else 200, media_type="application/xml; charset=utf-8",
                    headers={"Lock-Token": f"<{token}>"})


async def _unlock(request: Request, base: Path, target: Path, prefix: str) -> Response:
    return Response(status_code=204)
