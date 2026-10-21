"""Video analysis pipeline — probing, key-frame extraction and audio extraction.

Re-exports the helpers from :mod:`services.media_probe` so the precognition
code can import from a single ``video_analysis`` namespace.
"""
from __future__ import annotations

from services.media_probe import (
    ReelPolicy,
    cleanup_work_dir,
    detect_black_frames,
    detect_frozen_frames,
    extract_audio,
    extract_keyframes,
    measure_audio,
    probe,
    technical_check,
)

__all__ = [
    "ReelPolicy",
    "probe",
    "technical_check",
    "detect_black_frames",
    "detect_frozen_frames",
    "extract_keyframes",
    "extract_audio",
    "measure_audio",
    "cleanup_work_dir",
]
