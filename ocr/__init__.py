"""OCR extraction from key frames using Tesseract OCR (native).

This module uses Tesseract OCR directly via CLI, no external Python OCR libraries.
Supports Vietnamese + English, returns text with confidence and timestamps.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
from pathlib import Path

LOG = logging.getLogger("facebook-pipeline.ocr")


def extract_text(frames: list[Path]) -> str:
    """Extract text from frames using Tesseract OCR.

    Runs Tesseract on each frame and returns concatenated text.
    Returns empty string on any failure (logs error).

    Note: Timestamps and confidence are handled at the caller level
    via `extract_text_with_details()`.
    """
    if not frames:
        return ""

    all_text: list[str] = []
    for frame in frames:
        try:
            # Use tesseract with vie+eng, output JSON for confidence
            cmd = [
                "tesseract",
                str(frame),
                "stdout",
                "-l", "vie+eng",
                "--psm", "6",  # Assume uniform block of text
                "--oem", "1",  # LSTM only
                "tsv",  # Tab-separated values with confidence
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding='utf-8',
                timeout=30,
            )
            if result.returncode != 0:
                LOG.warning("Tesseract failed on %s: %s", frame.name, result.stderr[:200])
                continue

            # Parse TSV output
            text = _parse_tsv(result.stdout)
            if text:
                all_text.append(text)
        except subprocess.TimeoutExpired:
            LOG.warning("Tesseract timeout on %s", frame.name)
        except Exception as exc:
            LOG.warning("Tesseract error on %s: %s", frame.name, type(exc).__name__)

    return "\n".join(all_text) if all_text else ""


def extract_text_with_details(frames: list[Path]) -> list[dict]:
    """Extract text from frames with confidence and position info.

    Returns list of dicts per frame:
        {
            "frame": frame_path,
            "time": timestamp (from frame filename if available),
            "text": concatenated text,
            "confidence": average confidence (0-100),
            "words": [{"text": "...", "confidence": 95, "bbox": [x,y,w,h]}, ...]
        }
    """
    results = []
    for frame in frames:
        frame_result = {
            "frame": frame.name,
            "time": _extract_timestamp_from_filename(frame.name),
            "text": "",
            "confidence": 0.0,
            "words": [],
        }
        try:
            cmd = [
                "tesseract",
                str(frame),
                "stdout",
                "-l", "vie+eng",
                "--psm", "6",
                "--oem", "1",
                "tsv",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding='utf-8',
                timeout=30,
            )
            if result.returncode != 0:
                frame_result["error"] = f"Tesseract exit code {result.returncode}: {result.stderr[:200]}"
                results.append(frame_result)
                continue

            words = _parse_tsv_words(result.stdout)
            if words:
                frame_result["words"] = words
                frame_result["text"] = " ".join(w["text"] for w in words if w["text"].strip())
                confs = [w["confidence"] for w in words if w["confidence"] > 0]
                frame_result["confidence"] = sum(confs) / len(confs) if confs else 0.0
            else:
                frame_result["error"] = "No text detected"
        except subprocess.TimeoutExpired:
            frame_result["error"] = "Tesseract timeout"
        except Exception as exc:
            frame_result["error"] = f"{type(exc).__name__}: {exc}"

        results.append(frame_result)

    return results


def _parse_tsv(tsv_output: str) -> str:
    """Parse Tesseract TSV output and return concatenated text."""
    lines = tsv_output.strip().split("\n")
    if len(lines) < 2:
        return ""

    # Header: level, page_num, block_num, par_num, line_num, word_num, left, top, width, height, conf, text
    words = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 12:
            conf_str = parts[10]
            text = parts[11]
            try:
                conf = float(conf_str)
                if text and text.strip() and conf > 0:
                    words.append(text.strip())
            except ValueError:
                continue

    return " ".join(words) if words else ""


def _parse_tsv_words(tsv_output: str) -> list[dict]:
    """Parse Tesseract TSV output into word list with confidence and bbox."""
    lines = tsv_output.strip().split("\n")
    if len(lines) < 2:
        return []

    words = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 12:
            try:
                conf = float(parts[10])
                text = parts[11]
                if text and text.strip() and conf > 0:
                    words.append({
                        "text": text.strip(),
                        "confidence": conf,
                        "bbox": [
                            int(parts[6]),   # left
                            int(parts[7]),   # top
                            int(parts[8]),   # width
                            int(parts[9]),   # height
                        ],
                    })
            except (ValueError, IndexError):
                continue
    return words


def _extract_timestamp_from_filename(filename: str) -> float | None:
    """Extract timestamp from frame filename like 'frame-00.jpg' or 'frame-00_0.5s.jpg'."""
    # Try patterns like frame-00.jpg, frame-00_1.5s.jpg, etc.
    import re
    match = re.search(r"(\d+(?:\.\d+)?)s", filename)
    if match:
        return float(match.group(1))
    # Fallback: try frame-XX pattern as index
    match = re.search(r"frame[_-](\d+)", filename)
    if match:
        idx = int(match.group(1))
        # Assume 1 frame per second if no explicit timestamp
        return float(idx)
    return None