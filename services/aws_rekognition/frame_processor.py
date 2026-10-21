"""Frame extraction and AWS Rekognition analysis for video pre-check."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from services.aws_rekognition.client import RekognitionClient
from services.aws_rekognition.exceptions import (
    AccessDeniedError,
    BadRequestError,
    ProviderTimeoutError,
    RekognitionError,
    ThrottlingError,
)
from services.media_probe import ReelPolicy, extract_keyframes

LOG = logging.getLogger("facebook-pipeline.aws-rekognition.frames")

JPEG_MAX_BYTES = 5 * 1024 * 1024
MAX_FRAMES_DEFAULT = 10


@dataclass
class FrameResult:
    """One keyframe analysed by Rekognition."""
    time: float
    path: Path
    moderation_labels: list[dict] = field(default_factory=list)
    image_labels: list[dict] = field(default_factory=list)
    error: str = ""
    checked: bool = False


@dataclass
class FrameAnalysisReport:
    """Aggregated report for all keyframes of a video."""
    frames: list[FrameResult] = field(default_factory=list)
    total_calls: int = 0
    failed_calls: int = 0
    checked: bool = True
    error: str = ""
    total_frame_count: int = 0
    analysed_frame_count: int = 0
    overall: str = ""
    reason: str = ""

    @property
    def has_any_labels(self) -> bool:
        for f in self.frames:
            if f.moderation_labels or f.image_labels:
                return True
        return False


def _resize_jpeg(path: Path, output: Path, max_dim: int = 1280) -> Path:
    """Downscale to max 1280px on long edge, convert to JPEG."""
    import shutil
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    subprocess_run = __import__("subprocess").run
    subprocess_run([
        ffmpeg, "-y", "-i", str(path),
        "-vf", f"scale='min({max_dim},iw)':'min({max_dim},ih)':force_original_aspect_ratio=decrease",
        "-frames:v", "1",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", "5",
        str(output),
    ], check=True, capture_output=True)
    return output


def extract_and_analyze(
    video_path: Path,
    policy: ReelPolicy,
    client: RekognitionClient,
    work_dir: Path,
    max_frames: int = MAX_FRAMES_DEFAULT,
) -> FrameAnalysisReport:
    """Extract keyframes and run Rekognition on each."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    frames = extract_keyframes(video_path, count=min(max_frames, client.config.max_frames), work_dir=work_dir)

    report = FrameAnalysisReport(
        total_frame_count=len(frames),
        total_calls=client.call_count,
    )

    if not frames:
        report.checked = False
        report.error = "Không lấy được khung hình nào từ video"
        return report

    if not client.config.has_credentials:
        report.checked = False
        report.error = "AWS credentials not configured"
        return report

    for i, frame in enumerate(frames):
        frame_result = FrameResult(time=0.0, path=frame)
        try:
            # Resize to JPEG for Rekognition
            jpeg_path = work_dir / f"frame-{i:02d}.jpg"
            _resize_jpeg(frame, jpeg_path)
            image_bytes = jpeg_path.read_bytes()

            if len(image_bytes) > JPEG_MAX_BYTES:
                image_bytes = _resize_jpeg(frame, jpeg_path, max_dim=640).read_bytes()

            # DetectModerationLabels
            try:
                frame_result.moderation_labels = client.detect_moderation_labels(image_bytes)
            except AccessDeniedError:
                report.checked = False
                report.error = "AWS AccessDenied — IAM/SCP chặn Rekognition"
                return report
            except ThrottlingError:
                time.sleep(2)
                frame_result.moderation_labels = client.detect_moderation_labels(image_bytes)
            except ProviderTimeoutError:
                frame_result.error = "Timeout khi gọi Rekognition"

            # DetectLabels (only if no moderation labels found, for context)
            if not frame_result.moderation_labels and not frame_result.error:
                try:
                    frame_result.image_labels = client.detect_labels(image_bytes, max_labels=5)
                except (AccessDeniedError, ProviderTimeoutError, ThrottlingError):
                    pass

            frame_result.checked = True
            report.analysed_frame_count += 1

        except Exception as exc:
            LOG.error("Frame analysis error: %s", type(exc).__name__)
            frame_result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            frame_result.checked = False

        report.frames.append(frame_result)

    report.total_calls = client.call_count
    report.failed_calls = client.failed_count

    if report.analysed_frame_count == 0 and report.frames:
        report.checked = False
        report.error = "Tất cả frame đều lỗi — kiểm tra credentials/region"

    # Aggregate findings
    all_moderation = []
    for f in report.frames:
        all_moderation.extend(f.moderation_labels)

    if all_moderation:
        highest = max(all_moderation, key=lambda x: x["confidence"])
        report.overall = "high" if highest["confidence"] > 80 else "medium"
        report.reason = f"AWS Rekognition tìm thấy: {highest['name']} ({highest['confidence']:.1f}%)"
    elif report.checked:
        report.overall = "none"
        report.reason = "Không phát hiện nội dung vi phạm"
    else:
        report.overall = "unknown"
        report.reason = report.error or "Không thể phân tích"

    LOG.info(
        "Frame analysis complete: %d/%d frames checked, %d moderation labels, %d calls, %d failed",
        report.analysed_frame_count, report.total_frame_count,
        len(all_moderation), report.total_calls, report.failed_calls,
    )
    return report
