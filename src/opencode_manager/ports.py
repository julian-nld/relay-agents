"""Checking TCP ports on this machine."""

from __future__ import annotations

import socket


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if nothing on this machine is listening on `port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # As servers bind: connections of a service that just stopped (TIME_WAIT) don't
        # take the port, only a listener does. Without it a restarted service moves.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True
