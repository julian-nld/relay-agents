"""Helpers for agent subprocesses: draining stderr and stopping them."""

from __future__ import annotations

import collections
import os
import signal
import subprocess
import threading


class StderrTail:
    """Reads a process's stderr in the background, keeping the last lines.

    Draining it is necessary: a child that fills the stderr pipe (about 64 KB)
    blocks until someone reads, while we only read its stdout.
    """

    def __init__(self, stream, keep: int = 50) -> None:
        self.lines: collections.deque[str] = collections.deque(maxlen=keep)
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream) -> None:
        for line in stream:
            line = line.strip()
            if line:
                self.lines.append(line)

    def last(self, timeout: float = 2) -> str | None:
        self.thread.join(timeout)
        return self.lines[-1] if self.lines else None


def stop_process(process: subprocess.Popen | None, grace: float = 5) -> None:
    """Ask a process (started with start_new_session=True) and its children to
    stop with SIGINT, then kill them if they don't within `grace` seconds."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        pass
