"""Minimal JSON and server-sent-events client (standard library only)."""

from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.timeout = timeout

    def _open(self, method: str, path: str, body: Any = None, timeout: float | None = None):
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise HttpError(f"{method} {path}: HTTP {exc.code} {detail}", exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError(f"{method} {path}: {exc}") from exc

    def request(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
        with self._open(method, path, body, timeout=timeout or self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def events(self, path: str, stopped: threading.Event | None = None) -> Iterator[dict[str, Any]]:
        """Yield SSE payloads; cancellation wakes a blocked read, even on a silent server."""
        if stopped is not None and stopped.is_set():
            return
        # Bound connection/header waiting; once connected the stream may stay silent
        # indefinitely. A socket shutdown (not response.close from another thread)
        # wakes readline without waiting for its buffered-reader lock.
        with self._open("GET", path, timeout=self.timeout) as response:
            connection = response.fp.raw._sock
            connection.settimeout(None)
            finished = threading.Event()
            if stopped is not None:
                def cancel_read():
                    while not finished.wait(0.1):
                        if stopped.is_set():
                            try:
                                connection.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass  # already closed
                            return
                threading.Thread(target=cancel_read, name="opencode-event-cancel", daemon=True).start()
            try:
                data: list[str] = []
                for raw in response:
                    if stopped is not None and stopped.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        try:
                            yield json.loads("\n".join(data))
                        except json.JSONDecodeError:
                            pass
                        data = []
            except (OSError, http.client.HTTPException) as exc:
                if stopped is None or not stopped.is_set():
                    raise HttpError(f"GET {path}: {exc}") from exc
            finally:
                finished.set()
