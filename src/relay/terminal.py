"""A browser's interactive PTY, attached only to a project's WorkBox."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import uuid
from pathlib import Path

from relay.containers import Containers


def dimensions(data: dict) -> tuple[int, int]:
    try:
        cols, rows = int(data["cols"]), int(data["rows"])
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Expected terminal columns and rows") from exc
    if not 2 <= cols <= 1000 or not 1 <= rows <= 500:
        raise ValueError("Invalid terminal size")
    return cols, rows


class Terminal:
    def __init__(self, containers: Containers, directory: Path):
        self.containers = containers
        self.directory = directory.resolve()
        self.tag = uuid.uuid4().hex
        self.container = containers.name(self.directory)
        self.fd: int | None = None
        self.process: subprocess.Popen | None = None
        self.held = False
        self.size: tuple[int, int] | None = None

    def start(self, cols: int, rows: int) -> None:
        argv = self.containers.terminal_argv(self.directory, self.tag)
        self.held = True
        slave = None
        try:
            master, slave = pty.openpty()
            self.fd = master
            # Don't locally echo keystrokes arriving before podman puts its tty in raw mode.
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            self.resize(cols, rows)
            # Acquire the controlling tty after setsid, without preexec_fn in a threaded server.
            attach = ("import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
                      "os.execvp(sys.argv[1], sys.argv[1:])")
            self.process = subprocess.Popen([sys.executable, "-c", attach, *argv],
                                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                            cwd=self.directory)
            os.set_blocking(master, False)
        finally:
            if slave is not None:
                os.close(slave)

    def resize(self, cols: int, rows: int) -> None:
        if self.size == (cols, rows):
            return
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            self.size = cols, rows
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGWINCH)

    async def _ready(self, writing: bool = False) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def wake():
            if not ready.done():
                ready.set_result(None)

        add, remove = (loop.add_writer, loop.remove_writer) if writing else (loop.add_reader, loop.remove_reader)
        fd = self.fd
        add(fd, wake)
        try:
            await ready
        finally:
            remove(fd)

    async def read(self) -> bytes:
        while True:
            try:
                return os.read(self.fd, 16384)
            except BlockingIOError:
                await self._ready()
            except OSError as exc:
                if exc.errno == errno.EIO:  # Linux PTYs report EIO when the slave exits.
                    return b""
                raise

    async def write(self, data: str) -> None:
        pending = memoryview(data.encode())
        while pending:
            try:
                pending = pending[os.write(self.fd, pending):]
            except BlockingIOError:
                await self._ready(writing=True)

    def close(self) -> None:
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.held:
                self.held = False
                self.containers.close_terminal(self.container, self.tag)
        finally:
            if self.process:
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()


SCROLLBACK = 256 * 1024  # a kept shell's last output, shown again when it's opened again
KEPT_HOURS = 3  # a kept shell nobody opened for this long ends: as long as an unused WorkBox runs
TAKEN = object()  # (to the page that had a shell: another page has it now)


class Kept:
    """A chat's shell that outlives the page it was opened in: it goes on while nobody watches, its last output
    kept, and the chat's shell opened again is this one, until it exits, is ended, or is left too long."""

    def __init__(self, session: Terminal):
        self.session = session
        self.output = bytearray()
        self.cut = False  # (its first output dropped: more than SCROLLBACK)
        self.watcher: asyncio.Queue | None = None  # the page that has it: its output, then how it ended
        self.pump: asyncio.Task | None = None
        self.idle: asyncio.TimerHandle | None = None
        self.leaving: asyncio.Future | None = None  # (its end, once left too long)
        self.ended = False  # by End shell, not by its own exit
        self.closed = False

    def take(self, data: bytes) -> None:
        self.output += data
        if len(self.output) > SCROLLBACK:
            del self.output[:len(self.output) - SCROLLBACK]
            self.cut = True
        if self.watcher is not None:
            self.watcher.put_nowait(data)

    def replay(self) -> bytes:
        """What it showed lately; when that's cut, from a line's start (not inside a character or an escape)."""
        data = bytes(self.output)
        return data[data.find(b"\n") + 1:] if self.cut else data

    def attach(self) -> asyncio.Queue:
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        if self.watcher is not None:
            self.watcher.put_nowait(TAKEN)
        self.watcher = asyncio.Queue()
        return self.watcher


class Shells:
    """The chats' shells (Kept), one per conversation; `live` is every shell running (Relay.terminals)."""

    def __init__(self, live: set):
        self.live = live
        self.kept: dict[str, Kept] = {}
        self.keep_for = KEPT_HOURS * 3600

    def running(self, key: str) -> Kept | None:
        kept = self.kept.get(key)
        return kept if kept is not None and not kept.closed else None

    def keep(self, key: str, session: Terminal) -> Kept:
        """A shell just started (its PTY in `session`): kept, and read from now on, watched or not."""
        kept = Kept(session)
        self.kept[key] = kept
        kept.pump = asyncio.create_task(self._pump(key, kept))
        return kept

    async def _pump(self, key: str, kept: Kept) -> None:
        try:
            while data := await kept.session.read():
                kept.take(data)
        except OSError:
            pass
        await self._closed(key, kept)

    async def _closed(self, key: str, kept: Kept) -> None:
        if kept.closed:
            return
        kept.closed = True
        if self.kept.get(key) is kept:
            del self.kept[key]
        if kept.idle is not None:
            kept.idle.cancel()
        try:
            process = kept.session.process
            if process is not None and not kept.ended:
                await asyncio.to_thread(process.wait)  # (its own exit: its code)
            await asyncio.to_thread(kept.session.close)
        finally:
            self.live.discard(kept.session)
            code = kept.session.process.returncode if kept.session.process else None
            if kept.watcher is not None:
                kept.watcher.put_nowait(("exit", code, kept.ended))

    async def end(self, key: str, kept: Kept) -> None:
        """End Shell (and a chat deleted, a task discarded, Relay stopping, a shell left too long): its processes
        go, its WorkBox is free to stop."""
        if kept.closed:
            return
        kept.ended = True
        if kept.pump is not None:
            kept.pump.cancel()
            await asyncio.gather(kept.pump, return_exceptions=True)
        await self._closed(key, kept)

    async def end_for(self, key: str) -> None:
        if (kept := self.running(key)) is not None:
            await self.end(key, kept)

    def detach(self, key: str, kept: Kept, watcher: asyncio.Queue) -> None:
        """The page that had it is gone: it goes on, kept for a while."""
        if kept.watcher is not watcher:
            return  # (another page has it)
        kept.watcher = None
        if not kept.closed:
            kept.idle = asyncio.get_running_loop().call_later(self.keep_for, self._left, key, kept)

    def _left(self, key: str, kept: Kept) -> None:
        kept.leaving = asyncio.ensure_future(self.end(key, kept))

    async def close_all(self) -> None:
        await asyncio.gather(*(self.end(key, kept) for key, kept in list(self.kept.items())), return_exceptions=True)
