"""Storage for the media album of a workspace.

The album keeps its files in a private folder of the server. Nothing is written
into a public directory and nothing is served without an authorisation check.
A cloud adapter can be plugged in later, but the local one is the only one that
is used until it is configured.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_BYTES = 20 * 1024 * 1024
THUMBNAIL_MAX = 480

# The signature of every allowed format, checked against the first bytes.
SIGNATURES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}


class StorageError(RuntimeError):
    """The file could not be stored."""


@dataclass
class StoredMedia:
    storage_key: str
    mime_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    original_name: str
    thumbnail_key: str = ""


def album_root(base: Path) -> Path:
    """The private folder that holds every album."""
    root = Path(base) / "album"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def pipeline_folder(base: Path, pipeline_id: int) -> Path:
    """The folder of one workspace. Each pipeline owns its own album."""
    folder = album_root(base) / str(int(pipeline_id))
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def random_key(pipeline_id: int, suffix: str) -> str:
    """A storage key that cannot be guessed and never contains a user value."""
    return f"{int(pipeline_id)}/{secrets.token_hex(16)}{suffix}"


def safe_join(base: Path, storage_key: str) -> Path:
    """Resolve a storage key inside the album and refuse to leave it."""
    key = (storage_key or "").strip()
    if not key or key.startswith("/") or ".." in key:
        raise StorageError("Đường dẫn lưu trữ không hợp lệ")
    root = album_root(base).resolve()
    target = (root / key).resolve()
    if not str(target).startswith(str(root) + os.sep):
        raise StorageError("Đường dẫn lưu trữ không hợp lệ")
    return target


def detect_mime(data: bytes) -> str:
    """Read the real type of the file from its signature."""
    for signature, mime in SIGNATURES.items():
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_size(path: Path) -> tuple[int, int]:
    """Read the size of an image without an extra dependency."""
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=30)
        value = (completed.stdout or "").strip().splitlines()[0]
        width, height = value.split("x")[:2]
        return int(width), int(height)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, IndexError):
        return 0, 0


def make_thumbnail(source: Path, target: Path, size: int = THUMBNAIL_MAX) -> bool:
    """Create a small preview of the image."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
             "-vf", f"scale='min({size},iw)':-2", "-frames:v", "1", "-q:v", "4", str(target)],
            capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return completed.returncode == 0 and target.is_file() and target.stat().st_size > 0


def store_image(base: Path, pipeline_id: int, filename: str, content_type: str,
                data: bytes) -> StoredMedia:
    """Validate and store one image inside the album of a workspace."""
    if not data:
        raise StorageError("Tệp rỗng")
    if len(data) > MAX_BYTES:
        raise StorageError(f"Ảnh vượt giới hạn {MAX_BYTES // (1024 * 1024)} MB")
    mime = detect_mime(data)
    if not mime or mime not in ALLOWED_MIME:
        raise StorageError("Chỉ nhận ảnh JPEG, PNG hoặc WEBP")
    if content_type and content_type.split(";")[0].strip() != mime:
        raise StorageError("Kiểu tệp khai báo không khớp với nội dung tệp")
    suffix = ALLOWED_MIME[mime]
    key = random_key(pipeline_id, suffix)
    target = safe_join(base, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o600)
    width, height = image_size(target)
    thumbnail = ""
    if width and height:
        thumb_key = f"{int(pipeline_id)}/thumbs/{Path(key).stem}.jpg"
        thumb_target = safe_join(base, thumb_key)
        if make_thumbnail(target, thumb_target):
            thumb_target.chmod(0o600)
            thumbnail = thumb_key
    return StoredMedia(storage_key=key, mime_type=mime, size_bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(), width=width, height=height,
                        original_name=(filename or "")[:200], thumbnail_key=thumbnail)


def delete_media(base: Path, storage_key: str, thumbnail_key: str = "") -> None:
    """Remove a file from the album."""
    for key in (storage_key, thumbnail_key):
        if not key:
            continue
        try:
            target = safe_join(base, key)
        except StorageError:
            continue
        try:
            target.unlink()
        except OSError:
            continue


def album_usage(base: Path) -> int:
    """How much space the album uses."""
    total = 0
    for path in album_root(base).rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def clear_thumbnails(base: Path) -> None:
    """Remove every generated thumbnail."""
    for path in album_root(base).glob("*/thumbs"):
        shutil.rmtree(path, ignore_errors=True)
