"""Fingerprints of a media file, used to notice duplicates and near duplicates."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
HASH_WIDTH = 9
HASH_HEIGHT = 8
NEAR_DUPLICATE_DISTANCE = 10


def file_sha256(path: Path) -> str:
    """The exact hash of the file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_signature(path: Path, samples: int = 8) -> str:
    """A small difference hash of the picture, so a re-encoded copy still matches.

    The frames are reduced to nine by eight grey pixels and compared with their
    neighbour, which gives a signature that survives a change of quality.
    """
    frame_size = HASH_WIDTH * HASH_HEIGHT
    try:
        completed = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-vf", f"scale={HASH_WIDTH}:{HASH_HEIGHT}", "-frames:v", str(samples),
             "-pix_fmt", "gray", "-f", "rawvideo", "-"],
            capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    data = completed.stdout or b""
    if len(data) < frame_size:
        return ""
    bits: list[str] = []
    for offset in range(0, len(data) - frame_size + 1, frame_size):
        frame = data[offset:offset + frame_size]
        for row in range(HASH_HEIGHT):
            for column in range(HASH_WIDTH - 1):
                left = frame[row * HASH_WIDTH + column]
                right = frame[row * HASH_WIDTH + column + 1]
                bits.append("1" if left > right else "0")
    value = int("".join(bits[:64]), 2) if len(bits) >= 64 else 0
    return f"{value:016x}"


def hamming_distance(first: str, second: str) -> int:
    """How different two signatures are. A small number means the same video."""
    if not first or not second or len(first) != len(second):
        return 64
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1")
    except ValueError:
        return 64


def is_near_duplicate(first: str, second: str, threshold: int = NEAR_DUPLICATE_DISTANCE) -> bool:
    if not first or not second:
        return False
    return hamming_distance(first, second) <= threshold
