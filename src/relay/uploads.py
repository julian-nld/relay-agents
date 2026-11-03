"""Files people attach to messages."""

from __future__ import annotations

import mimetypes
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay import config, tunables
from relay.db import Database

MAX_BYTES: int | None = None  # set to override the setting (tests)


def max_bytes() -> int:
    return MAX_BYTES or tunables.get("uploads.max_mb") * 1024 * 1024
ORPHAN_SECONDS = 24 * 3600  # uploads never sent with a message are removed after this

TEXT_TYPES = ("application/json", "application/xml", "application/javascript",
              "application/x-yaml", "application/toml", "application/x-sh")


class UploadError(Exception):
    pass


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    mime: str
    size: int
    path: Path

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")

    @property
    def is_text(self) -> bool:
        return self.mime.startswith("text/") or self.mime in TEXT_TYPES

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "mime": self.mime, "size": self.size}


def uploads_dir() -> Path:
    return config.data_dir() / "uploads"


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    name = re.sub(r"[^\w.\- ()+@,]", "_", name)[:120]
    return "file" if name.strip(".") == "" else name  # no "", "." or ".."


def guess_mime(name: str, declared: str | None) -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    # (only a type as types go: it's sent back as a header, and a line break or any text was kept as it came)
    if declared != "application/octet-stream" and re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
                                                               r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}", declared):
        return declared
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class UploadWriter:
    """Streams one upload to its file, so large files never sit in memory."""

    def __init__(self, upload_id: str, name: str) -> None:
        self.upload_id = upload_id
        self.name = name
        self.folder = uploads_dir() / upload_id
        self.folder.mkdir(parents=True)
        self.path = self.folder / name
        self.size = 0
        self.file = open(self.path, "wb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > max_bytes():
            raise UploadError(f"Files can be at most {max_bytes() // (1024 * 1024)} MB")
        self.file.write(chunk)

    def close(self) -> None:
        self.file.close()

    def discard(self) -> None:
        self.file.close()
        shutil.rmtree(self.folder, ignore_errors=True)


class Uploads:
    def __init__(self, db: Database) -> None:
        self.db = db

    def store(self, user_id: int, name: str, mime: str | None, chunks) -> Attachment:
        """Save an upload given as `chunks` (bytes); enforce the size limit."""
        writer = self.writer(name)
        try:
            for chunk in chunks:
                writer.write(chunk)
        except BaseException:
            writer.discard()
            raise
        return self.register(user_id, writer, mime)

    def writer(self, name: str) -> UploadWriter:
        """Start writing an upload to disk, piece by piece."""
        return UploadWriter(uuid.uuid4().hex, safe_name(name))

    def register(self, user_id: int, writer: UploadWriter, mime: str | None) -> Attachment:
        writer.close()
        upload_id, name, size, path = writer.upload_id, writer.name, writer.size, writer.path
        attachment = Attachment(upload_id, name, guess_mime(name, mime), size, path)
        self.db.execute(
            "INSERT INTO uploads (id, user_id, name, mime, size, path, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (upload_id, user_id, attachment.name, attachment.mime, size, str(path), time.time()),
        )
        return attachment

    def get(self, upload_id: str, user_id: int) -> Attachment | None:
        row = self.db.one("SELECT * FROM uploads WHERE id = ? AND user_id = ?", (upload_id, user_id))
        return _attachment(row) if row else None

    def claim(self, ids: list[str], user_id: int, conversation_id: str) -> list[Attachment]:
        """Attach uploads to a conversation's message; they must be the user's and unused."""
        if len(ids) > tunables.get("uploads.per_message"):
            raise UploadError(f"At most {tunables.get('uploads.per_message')} attachments per message")
        found = []
        for upload_id in ids:
            row = self.db.one("SELECT * FROM uploads WHERE id = ? AND user_id = ?",
                              (str(upload_id), user_id))
            if row is None or row["conversation_id"] not in (None, conversation_id):
                raise UploadError("An attachment is missing; upload it again")
            found.append(_attachment(row))
        for attachment in found:
            self.db.execute("UPDATE uploads SET conversation_id = ? WHERE id = ?",
                            (conversation_id, attachment.id))
        return found

    def in_conversation(self, conversation_id: str, ids: list[str]) -> list[Attachment]:
        """The given uploads, as far as they belong to the conversation."""
        found = {a.id: a for a in self.for_conversation(conversation_id)}
        return [found[i] for i in ids if i in found and found[i].path.is_file()]

    def for_conversation(self, conversation_id: str) -> list[Attachment]:
        rows = self.db.all("SELECT * FROM uploads WHERE conversation_id = ?", (conversation_id,))
        return [_attachment(r) for r in rows]

    def copy(self, ids: list[str], source: str, target: str) -> dict[str, dict[str, Any]]:
        """Copy a conversation's uploads to another one (a fork); old id -> new public info."""
        copies = {}
        for attachment in self.in_conversation(source, ids):
            row = self.db.one("SELECT user_id FROM uploads WHERE id = ?", (attachment.id,))
            writer = self.writer(attachment.name)
            try:
                with open(attachment.path, "rb") as original:
                    while chunk := original.read(1024 * 1024):
                        writer.write(chunk)
            except BaseException:
                writer.discard()
                raise
            copy = self.register(row["user_id"], writer, attachment.mime)
            self.db.execute("UPDATE uploads SET conversation_id = ? WHERE id = ?", (target, copy.id))
            copies[attachment.id] = copy.public()
        return copies

    def delete_conversation(self, conversation_id: str) -> None:
        for attachment in self.for_conversation(conversation_id):
            shutil.rmtree(attachment.path.parent, ignore_errors=True)
        self.db.execute("DELETE FROM uploads WHERE conversation_id = ?", (conversation_id,))

    def purge_orphans(self) -> None:
        cutoff = time.time() - ORPHAN_SECONDS
        for row in self.db.all("SELECT * FROM uploads WHERE conversation_id IS NULL AND created < ?",
                               (cutoff,)):
            shutil.rmtree(Path(row["path"]).parent, ignore_errors=True)
        self.db.execute("DELETE FROM uploads WHERE conversation_id IS NULL AND created < ?", (cutoff,))


def _attachment(row: dict[str, Any]) -> Attachment:
    return Attachment(row["id"], row["name"], row["mime"], row["size"], Path(row["path"]))
