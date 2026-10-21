"""Authentication, session management and per-pipeline authorisation.

Design rules enforced here:

* Passwords and access keys are only ever stored as Argon2id hashes.
* Every secret is produced by the OS CSPRNG.
* A principal is resolved from the database on **every** request, so
  suspending a collaborator, changing their permissions or revoking a
  pipeline key takes effect immediately instead of waiting for a cookie
  to expire.
* A collaborator's authority is always derived from their membership row,
  never from a pipeline identifier supplied by the client.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# --- password / key hashing -------------------------------------------------

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16,
                         type=Type.ID)

SESSION_TTL_HOURS = 12
KEY_TTL_DAYS = 90
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 8

ROLE_ADMIN = "admin"
ROLE_CTV = "ctv"

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

PERMISSIONS = (
    "facebook.view",
    "facebook.add",
    "facebook.update_session",
    "groups.view",
    "groups.manage",
    "reels.create",
    "reels.publish",
    "group_posts.create",
    "group_posts.publish",
    "comments.manage",
    "ai_caption.use",
    "schedule.manage",
    "history.view",
    "media.view",
    "media.upload",
    "media.edit",
    "media.delete",
    "media.approve",
    "auto_post.configure",
    "auto_post.create",
    "auto_post.approve",
    "auto_post.publish",
    "auto_post.self_configure",
    "analytics.view",
)

# A new collaborator may look around and prepare drafts. Anything that
# publishes or touches credentials must be granted explicitly by an admin.
# A collaborator always configures their own automatic posting, so the
# permission for that is part of the defaults; the shared configuration of
# the pipeline stays behind "auto_post.configure".
DEFAULT_CTV_PERMISSIONS = ("facebook.view", "facebook.add", "groups.view", "groups.manage",
                           "media.view", "media.upload", "media.edit", "media.delete",
                           "history.view", "group_posts.create", "auto_post.self_configure")

PUBLISHING_PERMISSIONS = ("group_posts.publish", "reels.publish")


def hash_secret(secret: str) -> str:
    """Argon2id hash of a password or an access key."""
    return _hasher.hash(secret)


def verify_secret(stored_hash: str, secret: str) -> bool:
    """Constant-time verification against a stored Argon2id hash."""
    if not stored_hash or not secret:
        return False
    try:
        return _hasher.verify(stored_hash, secret)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return True


def token_urlsafe(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def new_pipeline_key() -> str:
    """A long, unguessable pipeline access key. Shown to the admin once."""
    raw = base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")
    return f"FPM-{raw}"


def key_prefix(key: str) -> str:
    """Short public fingerprint used to look the key row up before verifying."""
    return key[:12]


def hash_session_token(token: str) -> str:
    """Session cookies are random tokens; store only their SHA-256 digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    """Second-precision UTC timestamp, identical in shape to the app's own."""
    return (value or utcnow()).isoformat(timespec="seconds")


# --- permissions ------------------------------------------------------------

def normalise_permissions(values) -> set[str]:
    """Keep only known permission names; unknown names are rejected."""
    if isinstance(values, str):
        try:
            import json
            values = json.loads(values)
        except (ValueError, TypeError):
            values = []
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {value for value in values if isinstance(value, str) and value in PERMISSIONS}


# --- principal --------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    status: str
    pipeline_id: int | None
    permissions: frozenset[str] = field(default_factory=frozenset)
    session_id: int | None = None
    must_change_password: bool = False
    csrf_token: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def can(self, permission: str) -> bool:
        if self.is_admin:
            return True
        return permission in self.permissions

    def can_any(self, permissions) -> bool:
        if self.is_admin:
            return True
        return any(permission in self.permissions for permission in permissions)


# --- session helpers --------------------------------------------------------

def create_session(conn, user_id: int, pipeline_id: int | None, key_id: int | None,
                   session_ttl_hours: int = SESSION_TTL_HOURS) -> tuple[str, str, int]:
    """Create a session row and return (cookie token, csrf token, session id)."""
    token = token_urlsafe(48)
    csrf = token_urlsafe(32)
    expires = utcnow() + timedelta(hours=session_ttl_hours)
    cursor = conn.execute(
        """INSERT INTO user_sessions(user_id,pipeline_id,key_id,session_hash,csrf_token,created_at,expires_at)
           VALUES(?,?,?,?,?,?,?)""",
        (user_id, pipeline_id, key_id, hash_session_token(token), csrf, iso(), iso(expires)))
    return token, csrf, cursor.lastrowid


def resolve_principal(conn, cookie_token: str) -> Principal | None:
    """Rebuild the caller identity from the database. Never trust the cookie."""
    if not cookie_token:
        return None
    row = conn.execute(
        """SELECT s.id session_id, s.csrf_token, s.pipeline_id, s.key_id, s.expires_at, s.revoked_at,
                  u.id user_id, u.username, u.role, u.status, u.must_change_password
           FROM user_sessions s JOIN users u ON u.id=s.user_id
           WHERE s.session_hash=?""", (hash_session_token(cookie_token),)).fetchone()
    if row is None:
        return None
    if row["revoked_at"]:
        return None
    if row["expires_at"] and row["expires_at"] < iso():
        return None
    if row["status"] != STATUS_ACTIVE:
        return None
    pipeline_id = row["pipeline_id"]
    permissions: set[str] = set()
    if row["role"] == ROLE_CTV:
        membership = conn.execute(
            """SELECT pipeline_id, permissions_json, status FROM pipeline_memberships
               WHERE user_id=? AND pipeline_id=?""", (row["user_id"], pipeline_id)).fetchone()
        if membership is None or membership["status"] != STATUS_ACTIVE:
            return None
        pipeline = conn.execute("SELECT enabled FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
        if pipeline is None or not pipeline["enabled"]:
            return None
        permissions = normalise_permissions(membership["permissions_json"])
    return Principal(
        user_id=row["user_id"], username=row["username"], role=row["role"], status=row["status"],
        pipeline_id=pipeline_id, permissions=frozenset(permissions), session_id=row["session_id"],
        must_change_password=bool(row["must_change_password"]), csrf_token=row["csrf_token"])


def revoke_session(conn, session_id: int) -> None:
    conn.execute("UPDATE user_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (iso(), session_id))


def revoke_user_sessions(conn, user_id: int) -> int:
    cursor = conn.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                           (iso(), user_id))
    return cursor.rowcount


def revoke_key_sessions(conn, key_id: int) -> int:
    cursor = conn.execute("UPDATE user_sessions SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
                           (iso(), key_id))
    return cursor.rowcount


def purge_expired_sessions(conn) -> int:
    cursor = conn.execute("DELETE FROM user_sessions WHERE expires_at<? OR revoked_at IS NOT NULL", (iso(),))
    return cursor.rowcount


# --- login rate limiting ----------------------------------------------------

class LoginThrottle:
    """In-memory sliding window of failed logins per (ip, username)."""

    def __init__(self, window_seconds: int = LOGIN_WINDOW_SECONDS, max_failures: int = LOGIN_MAX_FAILURES):
        self.window_seconds = window_seconds
        self.max_failures = max_failures
        self._failures: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str, username: str) -> tuple[str, str]:
        return (ip or "-", (username or "-").lower())

    def is_blocked(self, ip: str, username: str) -> bool:
        now = time.time()
        with self._lock:
            attempts = [t for t in self._failures.get(self._key(ip, username), []) if now - t < self.window_seconds]
            if attempts:
                self._failures[self._key(ip, username)] = attempts
            else:
                self._failures.pop(self._key(ip, username), None)
            return len(attempts) >= self.max_failures

    def retry_after(self, ip: str, username: str) -> int:
        now = time.time()
        with self._lock:
            attempts = [t for t in self._failures.get(self._key(ip, username), []) if now - t < self.window_seconds]
        if not attempts:
            return 0
        return max(1, int(self.window_seconds - (now - attempts[0])))

    def record_failure(self, ip: str, username: str) -> None:
        now = time.time()
        with self._lock:
            key = self._key(ip, username)
            attempts = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
            attempts.append(now)
            self._failures[key] = attempts

    def clear(self, ip: str, username: str) -> None:
        with self._lock:
            self._failures.pop(self._key(ip, username), None)


LOGIN_THROTTLE = LoginThrottle()


# --- constant-time comparison helper ----------------------------------------

def secure_equals(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode(), (right or "").encode())
