"""Technical analysis of a video with ffprobe and ffmpeg.

Everything here is local: the file is probed, scanned for black or frozen
frames, measured for loudness and cut into key frames. Nothing is uploaded
anywhere. The output thresholds follow the current Reels requirements and can
be adjusted through the environment.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

MAX_PROBE_SECONDS = 90
TIMEOUT_SECONDS = 120


def _int_env(name: str, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, fallback))))
    except (TypeError, ValueError):
        return fallback


def _float_env(name: str, fallback: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.getenv(name, fallback))))
    except (TypeError, ValueError):
        return fallback


@dataclass
class ReelPolicy:
    """The requirements a video has to meet before it is worth publishing."""
    max_duration: float = field(default_factory=lambda: _float_env("REEL_MAX_DURATION", 90, 1, 900))
    min_duration: float = field(default_factory=lambda: _float_env("REEL_MIN_DURATION", 1, 0.1, 60))
    max_bytes: int = field(default_factory=lambda: _int_env("REEL_MAX_BYTES", 200 * 1024 * 1024, 1 << 20, 4 << 30))
    min_width: int = field(default_factory=lambda: _int_env("REEL_MIN_WIDTH", 480, 120, 4096))
    min_height: int = field(default_factory=lambda: _int_env("REEL_MIN_HEIGHT", 480, 120, 4096))
    preferred_width: int = field(default_factory=lambda: _int_env("REEL_PREFERRED_WIDTH", 1080, 120, 4096))
    preferred_height: int = field(default_factory=lambda: _int_env("REEL_PREFERRED_HEIGHT", 1920, 120, 4096))
    min_fps: float = field(default_factory=lambda: _float_env("REEL_MIN_FPS", 20, 1, 120))
    max_fps: float = field(default_factory=lambda: _float_env("REEL_MAX_FPS", 60, 5, 240))
    aspect_tolerance: float = field(default_factory=lambda: _float_env("REEL_ASPECT_TOLERANCE", 0.12, 0.01, 1.0))
    max_black_ratio: float = field(default_factory=lambda: _float_env("REEL_MAX_BLACK_RATIO", 0.4, 0, 1))
    max_freeze_seconds: float = field(default_factory=lambda: _float_env("REEL_MAX_FREEZE_SECONDS", 6, 0, 120))
    keyframes: int = field(default_factory=lambda: _int_env("REEL_KEYFRAMES", 6, 1, 16))
    require_audio: bool = field(default_factory=lambda: os.getenv("REEL_REQUIRE_AUDIO", "false").lower() == "true")
    max_silence_ratio: float = field(default_factory=lambda: _float_env("REEL_MAX_SILENCE_RATIO", 0.95, 0, 1))
    min_loudness: float = field(default_factory=lambda: _float_env("REEL_MIN_LOUDNESS", -50, -90, 0))
    max_loudness: float = field(default_factory=lambda: _float_env("REEL_MAX_LOUDNESS", -2, -40, 20))
    min_bitrate: int = field(default_factory=lambda: _int_env("REEL_MIN_BITRATE", 400_000, 0, 20_000_000))


def _run(command: list[str], timeout: int = TIMEOUT_SECONDS) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "not found"
    except Exception as error:  # pragma: no cover - defensive
        return 1, "", type(error).__name__


def _fraction(value) -> float:
    try:
        if not value or value in ("0/0", "N/A"):
            return 0.0
        if "/" in str(value):
            numerator, denominator = str(value).split("/", 1)
            denominator = float(denominator)
            return float(numerator) / denominator if denominator else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe(path: Path) -> dict:
    """Read the technical properties of a media file."""
    result = {"readable": False, "container": "", "video_codec": "", "audio_codec": "",
              "width": 0, "height": 0, "aspect": 0.0, "fps": 0.0, "duration": 0.0,
              "bitrate": 0, "audio_bitrate": 0, "has_audio": False, "size_bytes": 0,
              "errors": [], "warnings": [], "notes": []}
    path = Path(path)
    if not path.is_file():
        result["errors"].append("Tệp không tồn tại")
        return result
    result["size_bytes"] = path.stat().st_size
    code, stdout, stderr = _run([FFPROBE, "-v", "error", "-print_format", "json",
                                 "-show_format", "-show_streams", str(path)], timeout=MAX_PROBE_SECONDS)
    if code != 0 or not stdout.strip():
        result["errors"].append(f"ffprobe không đọc được tệp ({stderr.strip()[:160] or code})")
        return result
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        result["errors"].append("ffprobe trả về dữ liệu không đọc được")
        return result
    fmt = data.get("format", {}) or {}
    names = [name.strip() for name in (fmt.get("format_name") or "").split(",") if name.strip()]
    result["container"] = "mp4" if "mp4" in names else (names[0] if names else "")
    result["duration"] = _fraction(fmt.get("duration"))
    result["bitrate"] = int(_fraction(fmt.get("bit_rate")))
    streams = data.get("streams", []) or []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and not result["video_codec"]:
            result["video_codec"] = stream.get("codec_name", "")
            result["width"] = int(stream.get("width") or 0)
            result["height"] = int(stream.get("height") or 0)
            result["fps"] = _fraction(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
            result["aspect"] = round(result["width"] / result["height"], 4) if result["height"] else 0.0
            if not result["bitrate"]:
                result["bitrate"] = int(_fraction(stream.get("bit_rate")))
        elif kind == "audio" and not result["has_audio"]:
            result["has_audio"] = True
            result["audio_codec"] = stream.get("codec_name", "")
            result["audio_bitrate"] = int(_fraction(stream.get("bit_rate")))
    result["readable"] = bool(result["video_codec"])
    if not result["readable"]:
        result["errors"].append("Tệp không có luồng video đọc được")
    if not result["has_audio"]:
        # Silence is not a defect for a Reel, the audio category decides on it.
        result["notes"].append("Video không có âm thanh")
    return result


def technical_check(path: Path, policy: ReelPolicy | None = None) -> dict:
    """Check the file against the publishing requirements."""
    policy = policy or ReelPolicy()
    info = probe(path)
    errors = list(info["errors"])
    warnings = list(info["warnings"])
    reasons: list[str] = []
    suggestions: list[str] = []
    if info["readable"]:
        if info["size_bytes"] > policy.max_bytes:
            errors.append(f"Tệp nặng {info['size_bytes']} byte, vượt giới hạn {policy.max_bytes}")
            suggestions.append("Nén lại video, hãy giữ dưới giới hạn dung lượng")
        if info["duration"] > policy.max_duration:
            errors.append(f"Video dài {info['duration']:.1f}s, vượt giới hạn {policy.max_duration:.0f}s")
            suggestions.append("Cắt bớt video hoặc tăng giới hạn thời lượng")
        if info["duration"] and info["duration"] < policy.min_duration:
            errors.append(f"Video quá ngắn ({info['duration']:.1f}s)")
        if info["width"] < policy.min_width or info["height"] < policy.min_height:
            errors.append(f"Độ phân giải {info['width']}x{info['height']} thấp hơn yêu cầu "
                          f"{policy.min_width}x{policy.min_height}")
            suggestions.append("Xuất video ở độ phân giải cao hơn")
        else:
            preferred = 9 / 16
            if info["aspect"] and abs(info["aspect"] - preferred) > policy.aspect_tolerance:
                warnings.append(f"Tỉ lệ khung hình {info['aspect']:.3f} lệch khỏi 9:16")
                suggestions.append("Dựng video dọc 9:16 để hiển thị tốt hơn")
        if info["fps"] and (info["fps"] < policy.min_fps or info["fps"] > policy.max_fps):
            warnings.append(f"Khung hình {info['fps']:.2f} fps nằm ngoài {policy.min_fps:.0f}-{policy.max_fps:.0f}")
        if info["video_codec"] and info["video_codec"] not in ("h264", "hevc", "vp9", "av1"):
            warnings.append(f"Codec video '{info['video_codec']}' không phải lựa chọn tốt cho Reels")
            suggestions.append("Chuyển sang H.264 để tương thích tốt hơn")
        if info["container"] and info["container"] not in ("mp4", "mov", "m4v", "matroska", "webm"):
            warnings.append(f"Định dạng '{info['container']}' có thể không được hỗ trợ")
        if info["bitrate"] and info["bitrate"] < policy.min_bitrate:
            warnings.append(f"Bitrate {info['bitrate']} thấp, chất lượng có thể kém")
        if policy.require_audio and not info["has_audio"]:
            errors.append("Cấu hình yêu cầu video có âm thanh")
    status = "pass"
    if errors:
        status = "blocked"
    elif warnings:
        status = "needs_review"
    return {"status": status, "info": info, "reasons": errors + warnings,
            "errors": errors, "warnings": warnings, "suggestions": suggestions,
            "policy": {"max_duration": policy.max_duration, "min_duration": policy.min_duration,
                       "max_bytes": policy.max_bytes,
                       "min_resolution": f"{policy.min_width}x{policy.min_height}",
                       "fps_range": f"{policy.min_fps}-{policy.max_fps}"}}


def detect_black_frames(path: Path) -> dict:
    """Find long black stretches."""
    code, _, stderr = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
                            "-vf", "blackdetect=d=1.5:pix_th=0.10", "-an", "-f", "null", "-"],
                           timeout=MAX_PROBE_SECONDS * 2)
    if code not in (0, 1):
        return {"checked": False, "black_seconds": 0.0, "ratio": 0.0,
                "note": "Không chạy được bộ dò khung hình đen"}
    total = 0.0
    for match in re.finditer(r"black_duration:([0-9.]+)", stderr or ""):
        try:
            total += float(match.group(1))
        except ValueError:
            continue
    duration = probe(path).get("duration") or 0.0
    ratio = round(total / duration, 3) if duration else 0.0
    return {"checked": True, "black_seconds": round(total, 2), "ratio": ratio}


def detect_frozen_frames(path: Path) -> dict:
    """Find long stretches where the picture does not move."""
    code, _, stderr = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
                            "-vf", "freezedetect=n=-60dB:d=3", "-an", "-f", "null", "-"],
                           timeout=MAX_PROBE_SECONDS * 2)
    if code not in (0, 1):
        return {"checked": False, "frozen_seconds": 0.0, "ratio": 0.0,
                "note": "Không chạy được bộ dò đứng hình"}
    total = 0.0
    for match in re.finditer(r"freeze_duration:\s*([0-9.]+)", stderr or ""):
        try:
            total += float(match.group(1))
        except ValueError:
            continue
    duration = probe(path).get("duration") or 0.0
    ratio = round(total / duration, 3) if duration else 0.0
    return {"checked": True, "frozen_seconds": round(total, 2), "ratio": ratio}


def measure_audio(path: Path) -> dict:
    """Measure the loudness and the silent parts of the audio."""
    result = {"checked": False, "has_audio": False, "mean_volume": None, "max_volume": None,
              "silence_ratio": 0.0, "note": ""}
    info = probe(path)
    if not info.get("has_audio"):
        result["note"] = "Video không có âm thanh"
        return result
    code, _, stderr = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
                            "-af", "volumedetect", "-f", "null", "-"], timeout=MAX_PROBE_SECONDS * 2)
    if code not in (0, 1):
        result["note"] = "Không đo được âm lượng"
        result["checked"] = True
        result["has_audio"] = True
        return result
    mean = max_volume = None
    for match in re.finditer(r"mean_volume:\s*(-?[0-9.]+) dB", stderr or ""):
        try:
            mean = float(match.group(1))
        except ValueError:
            mean = None
    for match in re.finditer(r"max_volume:\s*(-?[0-9.]+) dB", stderr or ""):
        try:
            max_volume = float(match.group(1))
        except ValueError:
            max_volume = None
    silence = 0.0
    code2, _, stderr2 = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
                               "-af", "silencedetect=n=-45dB:d=1", "-f", "null", "-"],
                              timeout=MAX_PROBE_SECONDS * 2)
    for match in re.finditer(r"silence_duration:\s*([0-9.]+)", stderr2 or ""):
        try:
            silence += float(match.group(1))
        except ValueError:
            continue
    duration = info.get("duration") or 0.0
    result.update({"checked": True, "has_audio": True, "mean_volume": mean, "max_volume": max_volume,
                   "silence_ratio": round(silence / duration, 3) if duration else 0.0})
    return result


def extract_keyframes(path: Path, count: int, work_dir: Path) -> list[Path]:
    """Cut the video into a few key frames."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    duration = probe(path).get("duration") or 0.0
    if duration <= 0:
        return []
    count = max(1, min(int(count), 16))
    frames: list[Path] = []
    for index in range(count):
        offset = round(duration * (index + 0.5) / count, 2)
        target = work_dir / f"frame-{index:02d}.jpg"
        code, _, _ = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(offset),
                           "-i", str(path), "-frames:v", "1", "-q:v", "3", "-y", str(target)],
                          timeout=MAX_PROBE_SECONDS)
        if code == 0 and target.is_file() and target.stat().st_size > 0:
            frames.append(target)
    return frames


def extract_audio(path: Path, work_dir: Path) -> Path | None:
    """Extract the audio track as a small mono wav file."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "audio.wav"
    code, _, _ = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
                       "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-y", str(target)],
                      timeout=MAX_PROBE_SECONDS)
    if code == 0 and target.is_file() and target.stat().st_size > 1024:
        return target
    return None


def cleanup_work_dir(work_dir: Path) -> None:
    """Remove the temporary files of one check. The original file is kept."""
    work_dir = Path(work_dir)
    if not work_dir.exists():
        return
    for item in work_dir.glob("*"):
        try:
            if item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            continue
    try:
        work_dir.rmdir()
    except OSError:
        pass
