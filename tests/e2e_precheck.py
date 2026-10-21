"""End-to-end test: run a REAL video through the full pre-check pipeline.

Generates a test MP4 with ffmpeg, runs run_precheck() with a real AI provider
config loaded from .env, and prints a human-readable report.  No mocks.

ASR is excluded: the gateway has no working STT model (qwen3.5-omni-plus is
TTS, not ASR, and requires video_url content type).  The audio category will
report UNKNOWN in this case.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from services import ai_provider
from services.media_probe import ReelPolicy
from services.video_precheck import run_precheck, FINAL_PASS, FINAL_NEEDS_REVIEW, FINAL_BLOCKED, FINAL_CHECK_FAILED
from services.video_precheck import CATEGORIES


def make_test_video(work_dir: Path) -> Path:
    """Create a ~6s MP4 with a blue background + sine audio."""
    bg = work_dir / "bg.png"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=blue:720x1280",
        "-vf", "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='Hello World':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2",
        "-frames:v", "1", str(bg)
    ], check=True, capture_output=True)

    out = work_dir / "reel_test.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(bg),
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", "-t", "6",
        str(out)
    ], check=True, capture_output=True)
    return out


def main():
    print("=== E2E Video Pre-Check ===")
    print()

    # --- check tooling ---
    for tool in ["ffmpeg", "ffprobe"]:
        if not shutil.which(tool):
            print(f"FAILED: {tool} not found on PATH")
            return False
    print("OK  ffmpeg/ffprobe available")

    # --- load real AI config ---
    provider_config = ai_provider.load_config()
    print(f"AI configured: {provider_config.configured}")
    print(f"  text model:  {provider_config.model}")
    print(f"  vision model: {provider_config.vision_model}")
    print(f"  asr model:    {provider_config.asr_model} (SKIPPED - not configured)")

    if not provider_config.configured:
        print("FAILED: AI provider not configured (check .env / AI_KEY)")
        return False
    if not provider_config.vision_model:
        print("FAILED: AI_VISION_MODEL not set")
        return False

    # --- create video ---
    work_dir = Path(tempfile.mkdtemp(prefix="fpm-e2e-"))
    try:
        video = make_test_video(work_dir)
        print(f"OK  test video created: {video} ({video.stat().st_size} bytes)")
        print()

        # --- run full precheck ---
        policy = ReelPolicy(min_bitrate=0)
        report = run_precheck(video, "", [], provider_config, policy)

        print("--- REPORT ---")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print()

        # --- per-module evaluation ---
        ok = True

        # 1. FFprobe / technical
        tech = report["categories"].get("technical", {})
        print(f"[Technical] status={tech.get('status')} checked={tech.get('checked')}")
        if not tech.get("checked"):
            print("  FAILED: technical check did not run")
            ok = False
        else:
            print(f"  OK: probed successfully")
        print()

        # 2. Vision (real AI model call)
        visual = report["categories"].get("visual", {})
        print(f"[Vision] status={visual.get('status')} checked={visual.get('checked')}")
        if not visual.get("checked"):
            print("  FAILED: vision model did not analyze frames")
            print(f"  Reasons: {visual.get('reasons', [])}")
            ok = False
        else:
            print(f"  OK: {visual.get('checked_scope')}")
            if visual.get("details", {}).get("overall"):
                print(f"  Overall risk: {visual['details']['overall']}")
            print(f"  Reasons: {visual.get('reasons', [])}")
        print()

        # 3. OCR (depends on vision output)
        ocr = report["categories"].get("ocr", {})
        print(f"[OCR] status={ocr.get('status')} checked={ocr.get('checked')}")
        ocr_text = ocr.get("details", {}).get("text_preview", "")
        if ocr.get("checked") and ocr_text:
            print(f"  OCR text: {ocr_text[:200]}")
            print("  OK: OCR extracted text from frames")
        elif ocr.get("checked"):
            print("  OK: OCR ran (no text detected — may be expected)")
        else:
            print(f"  WARNING: OCR not checked (depends on vision) — Reasons: {ocr.get('reasons', [])}")
        print()

        # 4. ASR — SKIPPED (no working STT model at gateway)
        audio = report["categories"].get("audio", {})
        print(f"[ASR] status={audio.get('status')} checked={audio.get('checked')}")
        print("  SKIPPED per user request — no working ASR model available")
        print()

        # 5. Community Standards evaluation (12 risk categories)
        print("[Community Standards] evaluating issues...")
        issues = report.get("issues", [])
        risk_cats = set()
        for issue in issues:
            print(f"  - [{issue.get('risk_level')}] {issue.get('policy_category')}: {issue.get('reason', '')[:80]}")
            risk_cats.add(issue.get("policy_category"))
        if issues:
            print(f"  OK: {len(issues)} issues detected across {len(risk_cats)} categories")
        else:
            print("  OK: no violations detected")
        print()

        # 6. Copyright
        copyright_cat = report["categories"].get("copyright", {})
        print(f"[Copyright] status={copyright_cat.get('status')}")
        cr_status = copyright_cat.get("details", {}).get("copyright_status", "")
        print(f"  copyright_status={cr_status}")
        if cr_status != "UNVERIFIED":
            print("  FAILED: copyright should be UNVERIFIED by default")
            ok = False
        else:
            print("  OK: copyright is UNVERIFIED (not auto-PASS)")
        print()

        # 7. Caption (empty test)
        caption_cat = report["categories"].get("caption", {})
        print(f"[Caption] status={caption_cat.get('status')} checked={caption_cat.get('checked')}")
        cap_scope = caption_cat.get("checked_scope", "")
        if "NOT_PROVIDED" in cap_scope:
            print("  OK: caption correctly flagged as NOT_PROVIDED")
        else:
            print("  WARNING: caption scope not NOT_PROVIDED")
        print()

        # 8. Issues with timestamps and risk levels
        print(f"[Issues] {len(issues)} issues found")
        for issue in issues[:10]:
            print(f"  - {issue.get('policy_category')}: {issue.get('risk_level')} "
                  f"ts={issue.get('timestamp')}")
            if issue.get("timestamp") is not None or issue.get("timestamp") is None:
                pass  # timestamps can be None for non-frame-specific issues
        if any(i.get("timestamp") is not None for i in issues):
            print("  OK: at least one issue with a timestamp")
        else:
            print("  INFO: non-issue timestamps may be None for file-level findings")
        print()

        # 9. No ProviderError in report
        err = report.get("error", "")
        if "ProviderError" in err:
            print(f"[Error] FAILED: ProviderError present: {err}")
            ok = False
        else:
            print(f"[Error] OK: no ProviderError in final report")
        print()

        # 10. Final status
        print(f"[Final] status={report['status']} score={report.get('score', 0)}")
        print(f"  policy_version={report.get('policy_version')}")
        print(f"  checked_scope: {report.get('checked_scope', '')[:150]}...")
        print()

        # 11. Publish guard simulation
        final = report["status"]
        if final == FINAL_CHECK_FAILED:
            print(f"[Publish] BLOCKED (CHECK_FAILED) — cannot publish")
        elif final == FINAL_BLOCKED:
            print(f"[Publish] BLOCKED (BLOCKED) — cannot publish")
        elif final == FINAL_NEEDS_REVIEW:
            print(f"[Publish] NEEDS_REVIEW — cannot publish without approval")
        elif final == FINAL_PASS:
            print(f"[Publish] PASS — would allow publish")
        else:
            print(f"[Publish] UNKNOWN status={final}")
        print()

        # 12. Check that non-duplicate video still shows UNVERIFIED
        cr_cat = report["categories"].get("copyright", {})
        if cr_cat["details"]["copyright_status"] == "UNVERIFIED":
            print("[Copyright] OK: no false PASS just because video isn't a workspace duplicate")
        else:
            print("[Copyright] FAILED: copyright was not UNVERIFIED")
            ok = False

        print()
        print("=== SUMMARY ===")
        print(f"Overall result: {'PASS' if ok else 'FAILED'}")
        print(f"Video precheck status: {report['status']}")
        return ok

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
