"""Accounts, password hashing, login sessions, app passwords and login rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from relay.db import Database

MIN_PASSWORD_LENGTH = 8
NO_PASSWORD = "!"  # password hash of the 'local' account used when login is off
# An app password says what it is (in a script, a log, a secret scanner's eyes) and is never a session's token
APP_PASSWORD_PREFIX = "relay_"
APP_PASSWORD_USED_EVERY = 60  # seconds: how often its last use is written down (not at every call)
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), dklen=32, n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(candidate.hex(), digest)


def token_hash(token: str) -> str:
    """A session's key in auth_sessions (the token itself is kept nowhere)."""
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, db: Database) -> None:
        self.db = db

    def has_users(self) -> bool:
        """True once a real account exists (not just the login-less 'local' one)."""
        return self.db.one(
            "SELECT 1 FROM users WHERE password_hash != ? LIMIT 1", (NO_PASSWORD,)) is not None

    def claim_local_user(self, username: str, password: str) -> int | None:
        """Turn the login-less 'local' account into a real one, keeping its
        conversations; None if there is no such account."""
        local = self.db.one("SELECT id FROM users WHERE password_hash = ? ORDER BY id LIMIT 1",
                            (NO_PASSWORD,))
        if local is None:
            return None
        username = username.strip()
        if not username:
            raise AuthError("Username is required")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        self.db.execute("UPDATE users SET username = ?, password_hash = ? WHERE id = ?",
                        (username, hash_password(password), local["id"]))
        return local["id"]

    def local_user(self) -> dict:
        """The account used when login is disabled (created on first use)."""
        user = self.db.one("SELECT id, username FROM users ORDER BY id LIMIT 1")
        if user is None:
            cursor = self.db.execute(
                "INSERT INTO users (username, password_hash, created) VALUES ('local', ?, ?)",
                (NO_PASSWORD, time.time()),
            )
            user = {"id": cursor.lastrowid, "username": "local"}
        return user

    def create_user(self, username: str, password: str) -> int:
        username = username.strip()
        if not username:
            raise AuthError("Username is required")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise AuthError(f"User {username!r} already exists")
        cursor = self.db.execute(
            "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
            (username, hash_password(password), time.time()),
        )
        return cursor.lastrowid

    def set_password(self, username: str, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        user = self.db.one("SELECT id FROM users WHERE username = ?", (username,))
        if user is None:
            raise AuthError(f"No user {username!r}")
        with self.db.transaction() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (hash_password(password), user["id"]))
            # Changing the password signs out every session.
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user["id"],))

    def authenticate(self, username: str, password: str) -> dict | None:
        user = self.db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if user is None:
            verify_password(password, hash_password("x" * MIN_PASSWORD_LENGTH))  # same timing
            return None
        return user if verify_password(password, user["password_hash"]) else None

    def start_session(self, user_id: int, days: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created, expires) VALUES (?, ?, ?, ?)",
            (token_hash(token), user_id, now, now + days * 86400),
        )
        return token

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.one(
            "SELECT u.id, u.username, s.expires FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (token_hash(token),),
        )
        if row is None or row["expires"] < time.time():
            return None
        return {"id": row["id"], "username": row["username"]}

    def end_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash(token),))

    def purge_expired(self) -> None:
        self.db.execute("DELETE FROM auth_sessions WHERE expires < ?", (time.time(),))

    # App passwords: one per script or program, used instead of the account's password, revoked one by one. The
    # password itself is shown once and kept nowhere (its hash is).

    def create_app_password(self, user_id: int, name: str) -> dict:
        name = " ".join(name.split())[:80]
        if not name:
            raise AuthError("Name it after what will use it, e.g. backup script")
        password = APP_PASSWORD_PREFIX + secrets.token_urlsafe(32)
        made = {"id": secrets.token_hex(8), "name": name, "created": time.time(), "last_used": None}
        self.db.execute("INSERT INTO app_passwords (id, user_id, name, token_hash, created) VALUES (?, ?, ?, ?, ?)",
                        (made["id"], user_id, name, token_hash(password), made["created"]))
        return {**made, "password": password}

    def app_passwords(self, user_id: int) -> list[dict]:
        return self.db.all("SELECT id, name, created, last_used FROM app_passwords WHERE user_id = ? ORDER BY created",
                           (user_id,))

    def remove_app_password(self, user_id: int, password_id: str) -> bool:
        return self.db.execute("DELETE FROM app_passwords WHERE id = ? AND user_id = ?",
                               (password_id, user_id)).rowcount > 0

    def user_for_app_password(self, password: str | None, username: str | None = None) -> dict | None:
        """The account an app password is for ({id, username, app_password: its id}), or None. With `username`
        (HTTP Basic), it must be that account's."""
        if not password or not password.startswith(APP_PASSWORD_PREFIX):
            return None
        row = self.db.one("SELECT a.id AS app, a.last_used, u.id, u.username FROM app_passwords a "
                          "JOIN users u ON u.id = a.user_id WHERE a.token_hash = ?", (token_hash(password),))
        if row is None or (username is not None and username != row["username"]):
            return None
        now = time.time()
        if not row["last_used"] or now - row["last_used"] > APP_PASSWORD_USED_EVERY:
            self.db.execute("UPDATE app_passwords SET last_used = ? WHERE id = ?", (now, row["app"]))
        return {"id": row["id"], "username": row["username"], "app_password": row["app"]}


class LoginThrottle:
    """Slows down password guessing: after a few failures from one client,
    further attempts are refused for a growing period."""

    FREE_ATTEMPTS = 5

    def __init__(self) -> None:
        self.failures: dict[str, tuple[int, float]] = {}
        self.pending: dict[str, int] = {}
        self.lock = threading.Lock()

    def begin(self, client: str) -> float:
        """Reserve an attempt; returns seconds to wait (0 = go ahead).

        Attempts still being checked count as failures, so parallel requests
        can't all slip past the limit.
        """
        with self.lock:
            count, until = self.failures.get(client, (0, 0.0))
            wait = until - time.time()
            if wait > 0:
                return wait
            # Free attempts that are left may run in parallel; past the limit
            # (after the lockout expired) only one attempt at a time.
            allowed = max(1, self.FREE_ATTEMPTS - count)
            if self.pending.get(client, 0) >= allowed:
                return 1.0
            self.pending[client] = self.pending.get(client, 0) + 1
            return 0.0

    def _end(self, client: str) -> None:
        left = self.pending.get(client, 1) - 1
        if left > 0:
            self.pending[client] = left
        else:
            self.pending.pop(client, None)

    def failed(self, client: str) -> None:
        with self.lock:
            self._end(client)
            count, _ = self.failures.get(client, (0, 0.0))
            count += 1
            delay = 0 if count < self.FREE_ATTEMPTS else min(2 ** (count - self.FREE_ATTEMPTS) * 5, 900)
            self.failures[client] = (count, time.time() + delay)

    def succeeded(self, client: str) -> None:
        with self.lock:
            self._end(client)
            self.failures.pop(client, None)
