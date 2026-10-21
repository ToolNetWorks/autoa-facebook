"""Exclusive per-account browser profile lock.

One Chromium persistent profile may only be driven by one process at a time.
Two Playwright sessions on the same profile corrupt the cookie database, so
every browser entry point takes an advisory `flock` on a per-account lock file
before touching the profile.

The lock is held by the operating system, not by this process, so it is
released automatically if the holder crashes. The lock file also records the
holder PID so the dashboard can report which profile is busy.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from pathlib import Path


class ProfileBusy(RuntimeError):
    """Raised when another process already drives this account's browser profile."""


def lock_dir(data_dir: Path) -> Path:
    folder = Path(data_dir) / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def lock_path(account_id: int, data_dir: Path) -> Path:
    return lock_dir(data_dir) / f"account-{int(account_id)}.lock"


class ProfileLock:
    """Advisory exclusive lock for one account's Chromium profile."""

    def __init__(self, account_id: int, data_dir: Path):
        self.account_id = int(account_id)
        self.data_dir = Path(data_dir)
        self.path = lock_path(self.account_id, self.data_dir)
        self._handle = None

    def acquire(self) -> "ProfileLock":
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise ProfileBusy(
                    f"Hồ sơ trình duyệt của tài khoản #{self.account_id} đang được một phiên khác sử dụng"
                ) from error
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "since": time.time()}))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            # Clear the holder record so the dashboard stops reporting a
            # busy profile as soon as the lock is given up.
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
        except OSError:
            pass
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()


def holder(data_dir: Path, account_id: int) -> dict | None:
    """Return details of the live process holding this profile, if any."""
    path = lock_path(account_id, data_dir)
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not content:
        return None
    try:
        info = json.loads(content)
    except json.JSONDecodeError:
        return None
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        os.kill(pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return None
        if error.errno != errno.EPERM:
            return None
    return {"pid": pid, "since": info.get("since")}


def profile_busy(data_dir: Path, account_id: int) -> bool:
    """True when another process currently drives this account's profile."""
    return holder(data_dir, account_id) is not None
