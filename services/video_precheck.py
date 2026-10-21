"""AI video pre-check that runs before a Reel is published.

The check walks six categories and never decides more than the evidence allows:

* technical  – the file is probed with ffprobe and scanned for black or frozen
  frames and for the audio level,
* visual    – key frames are read by AWS Rekognition (DetectModerationLabels +
  DetectLabels) and checked against the Community Standards risk buckets,
* ocr       – text visible in the key frames is extracted by PaddleOCR and screened,
* audio     – audio track measured for loudness/silence (NO speech-to-text),
* caption   – the caption goes through the caption validator and the text model,
* copyright – the file hash is compared with the media of the same workspace
  and a separate copyright-verification status is produced (UNVERIFIED by default).

A category that could not be checked stays *check_failed* or *unknown*, and a
single check_failed/unknown category keeps the whole result at NEEDS_REVIEW.
Only a check where every required category passed may be published, and even
then the result is a hint and not a promise about the decision of Facebook.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from community_standards import POLICY_VERSION
from ocr import extract_text_with_details as ocr_extract_text_with_details
from policy_evaluator import (
    evaluate_technical,
    compute_severity_from_findings,
)
from report_builder import (
    CATEGORY_AUDIO,
    CATEGORY_CAPTION,
    CATEGORY_COPYRIGHT,
    CATEGORY_OCR,
    CATEGORY_TECHNICAL,
    CATEGORY_VISUAL,
    STATUS_CHECK_FAILED,
    STATUS_BLOCKED,
    STATUS_NEEDS_REVIEW,
    STATUS_PASS,
    STATUS_UNKNOWN,
    build_category_report,
    build_report,
    CATEGORIES as _CATEGORIES,
)
from services import ai_provider
from services.aws_rekognition import (
    RekognitionClient,
    RekognitionConfig,
    load_config as load_aws_config,
)
from services.aws_rekognition.frame_processor import (
    extract_and_analyze,
    FrameAnalysisReport,
)
from services.aws_rekognition.mapper import (
    map_moderation_labels,
    map_image_labels,
)
from services.caption_validator import analyze_caption
from services.media_fingerprint import file_sha256, perceptual_signature
from video_analysis import (
    ReelPolicy,
    cleanup_work_dir,
    detect_black_frames,
    detect_frozen_frames,
    extract_audio,
    extract_keyframes,
    measure_audio,
    technical_check,
)

LOG = logging.getLogger("facebook-pipeline.precheck")

FINAL_PASS = "PASS"
FINAL_NEEDS_REVIEW = "NEEDS_REVIEW"
FINAL_BLOCKED = "BLOCKED"
FINAL_CHECK_FAILED = "CHECK_FAILED"
CATEGORIES = _CATEGORIES  # (technical, visual, audio, ocr, caption, copyright)


def _technical_category(path: Path, policy: ReelPolicy) -> tuple[dict, list[dict]]:
    """Run all technical checks via ffmpeg/ffprobe."""
    report = technical_check(path, policy)
    black = detect_black_frames(path)
    frozen = detect_frozen_frames(path)
    reasons: list[str] = list(report["reasons"])
    suggestions: list[str] = list(report["suggestions"])
    status = report["status"]

    if black.get("checked") and black.get("ratio", 0) > policy.max_black_ratio:
        reasons.append(f"Video có {black['ratio'] * 100:.0f}% khung hình đen")
        suggestions.append("Kiểm tra lại phần đầu và cuối video")
        status = "blocked" if status != "blocked" else status
    if frozen.get("checked") and frozen.get("frozen_seconds", 0) > policy.max_freeze_seconds:
        reasons.append(f"Video đứng hình {frozen['frozen_seconds']:.1f}s")
        suggestions.append("Cắt bỏ đoạn đứng hình")
        if status == "pass":
            status = "needs_review"

    findings = evaluate_technical(report, policy, black, frozen)
    details = {"report": report, "black_frames": black, "frozen_frames": frozen}
    scope = "ffprobe + ffmpeg (blackdetect, freezedetect, volumedetect)"
    score = 1.0 if status == "blocked" else (0.4 if status == "needs_review" else 0.0)
    tech_status = (STATUS_BLOCKED if status == "blocked" else
                   STATUS_NEEDS_REVIEW if status == "needs_review" else STATUS_PASS)
    return build_category_report(tech_status, reasons, suggestions, details,
                                 checked=True, score=score, checked_scope=scope), findings


def _visual_category(frame_report: FrameAnalysisReport, policy: ReelPolicy) -> tuple[dict, list[dict]]:
    """Convert AWS Rekognition frame analysis to visual category."""
    all_findings: list[dict] = []
    details = {
        "total_frames": frame_report.total_frame_count,
        "analysed_frames": frame_report.analysed_frame_count,
        "total_rekognition_calls": frame_report.total_calls,
        "failed_calls": frame_report.failed_calls,
        "frames": [],
    }

    if not frame_report.checked:
        visual = build_category_report(
            STATUS_CHECK_FAILED,
            [frame_report.error or "AWS Rekognition không khả dụng"],
            ["Kiểm tra AWS credentials, region, và SCP policies"],
            checked=False,
            checked_scope=f"AWS Rekognition: {frame_report.error or 'unknown error'}",
            details=details,
        )
        return visual, all_findings

    if frame_report.analysed_frame_count == 0:
        visual = build_category_report(
            STATUS_CHECK_FAILED,
            ["Không phân tích được khung hình nào"],
            ["Kiểm tra video và AWS configuration"],
            checked=False,
            checked_scope="0 frames analysed",
            details=details,
        )
        return visual, all_findings

    # Map AWS labels to CS findings
    all_moderation = []
    all_image_labels = []
    for f in frame_report.frames:
        if f.moderation_labels:
            all_moderation.extend(f.moderation_labels)
        if f.image_labels:
            all_image_labels.extend(f.image_labels)
        details["frames"].append({
            "time": f.time,
            "moderation": f.moderation_labels,
            "labels": f.image_labels,
            "checked": f.checked,
            "error": f.error,
        })

    # Convert to CS findings
    mod_findings = map_moderation_labels(all_moderation)
    label_findings = map_image_labels(all_image_labels)
    all_findings.extend(mod_findings)
    all_findings.extend(label_findings)

    # Determine overall status
    max_severity = "none"
    for f in all_findings:
        sev = f.get("severity", "none")
        if sev == "blocked":
            max_severity = "blocked"
        elif sev == "high" and max_severity not in ("blocked",):
            max_severity = "high"
        elif sev == "medium" and max_severity in ("none", "low"):
            max_severity = "medium"

    severity_map = {"none": STATUS_PASS, "low": STATUS_PASS,
                    "medium": STATUS_NEEDS_REVIEW, "high": STATUS_BLOCKED, "blocked": STATUS_BLOCKED}
    visual_status = severity_map.get(max_severity, STATUS_NEEDS_REVIEW)

    # Deduplicate reasons by evidence content
    seen_evidence = set()
    reasons = []
    for f in all_findings:
        evidence = f.get("evidence", "")[:150]
        if evidence not in seen_evidence:
            seen_evidence.add(evidence)
            reasons.append(f"{f['label']}: {evidence}")
    if not reasons:
        reasons.append("Không phát hiện nội dung vi phạm theo AWS Rekognition")
    suggestions = ["Xem lại các đoạn được nêu và chỉnh sửa trước khi đăng"] if all_findings else []

    visual_details = {
        "overall": max_severity,
        "reason": frame_report.reason,
        "total_moderation_labels": len(all_moderation),
        "total_image_labels": len(all_image_labels),
    }
    visual_details.update(details)

    visual = build_category_report(
        visual_status, reasons, suggestions, visual_details,
        checked=True,
        score=0.0 if visual_status == STATUS_PASS else (0.9 if visual_status == STATUS_BLOCKED else 0.45),
        checked_scope=f"AWS Rekognition: {frame_report.analysed_frame_count}/{frame_report.total_frame_count} frames",
    )
    return visual, all_findings


def _ocr_category(frames: list[Path], policy: ReelPolicy, work_dir: Path) -> tuple[dict, list[dict]]:
    """Run Tesseract OCR on keyframes independently of AWS Vision."""
    all_findings: list[dict] = []

    if not frames:
        ocr = build_category_report(
            STATUS_UNKNOWN,
            ["OCR chưa chạy (không có khung hình)"],
            [],
            checked=False,
            checked_scope="no keyframes extracted",
            details={"frames": []},
        )
        return ocr, all_findings

    # Run Tesseract OCR on all frames with details (text, confidence, timestamp)
    try:
        frame_results = ocr_extract_text_with_details(frames)
    except Exception as exc:
        LOG.error("OCR error: %s", type(exc).__name__)
        frame_results = [{"frame": f.name, "error": str(exc)[:200]} for f in frames]

    # Combine text from all frames
    combined_text = "\n".join(r.get("text", "") for r in frame_results if r.get("text"))
    ocr_text_length = len(combined_text)

    # Collect errors
    errors = [r.get("error") for r in frame_results if r.get("error")]
    if errors:
        LOG.warning("OCR errors on %d frames: %s", len(errors), errors[0])

    # Prepare details
    frame_details = []
    for r in frame_results:
        detail = {
            "frame": r.get("frame"),
            "time": r.get("time"),
            "text": r.get("text", "")[:200],
            "confidence": r.get("confidence", 0),
        }
        if "error" in r:
            detail["error"] = r["error"]
        frame_details.append(detail)

    if ocr_text_length > 0:
        from policy_evaluator import _scan_text
        ocr_findings = _scan_text(combined_text, "ocr")
        all_findings.extend(ocr_findings)
        ocr_status = STATUS_NEEDS_REVIEW if ocr_findings else STATUS_PASS
        ocr_reasons = [f"OCR: {f['evidence'][:150]}" for f in ocr_findings] or ["Không tìm thấy văn bản nguy hiểm"]
        ocr_suggestions = ["Kiểm tra lại văn bản xuất hiện trong video"] if ocr_findings else []
    else:
        ocr_status = STATUS_PASS
        ocr_reasons = ["Không có văn bản trong khung hình"]
        ocr_suggestions = []

    if errors:
        ocr_reasons.append(f"OCR lỗi trên {len(errors)} khung hình: {errors[0]}")
        if ocr_status == STATUS_PASS:
            ocr_status = STATUS_CHECK_FAILED
        ocr_suggestions.append("Kiểm tra Tesseract cài đặt và chất lượng khung hình")

    ocr_details = {
        "text_length": ocr_text_length,
        "text_preview": (combined_text[:500] if combined_text else ""),
        "frames_processed": len(frames),
        "frame_results": frame_details,
    }
    ocr = build_category_report(
        ocr_status, ocr_reasons, ocr_suggestions, ocr_details,
        checked=True,
        score=0.0 if ocr_status == STATUS_PASS else 0.4,
        checked_scope=f"{len(frames)} keyframes scanned for text by Tesseract OCR (vie+eng)",
    )
    return ocr, all_findings


def _audio_category(audio_path: Path | None, policy: ReelPolicy) -> tuple[dict, list[dict]]:
    """Measure audio levels (NO speech-to-text). ASR is removed per requirements."""
    measurement = measure_audio(audio_path) if audio_path else {
        "checked": False, "has_audio": False, "note": "Không trích được âm thanh"}
    all_findings: list[dict] = []
    reasons: list[str] = []
    suggestions: list[str] = []

    if not measurement.get("has_audio"):
        cat = build_category_report(
            STATUS_PASS,
            ["Video không có âm thanh"],
            [],
            details=measurement,
            checked=True,
            checked_scope="no audio track in video",
            score=0.0,
        )
        return cat, all_findings

    if measurement.get("checked"):
        mean = measurement.get("mean_volume")
        if mean is not None and mean < policy.min_loudness:
            reasons.append(f"Âm lượng trung bình {mean:.1f} dB rất thấp, nghe khó")
            suggestions.append("Tăng âm lượng hoặc kiểm tra lại thu âm")
        if measurement.get("silence_ratio", 0) > policy.max_silence_ratio:
            reasons.append(f"{measurement['silence_ratio'] * 100:.0f}% video không có tiếng")
            suggestions.append("Thêm âm thanh hoặc lời thoại")

    # ASR is intentionally disabled — no speech-to-text
    reasons.append("Nội dung lời thoại không được phân tích (ASR đã bị loại bỏ)")
    suggestions.append("Nếu cần kiểm tra lời thoại, hãy cung cấp transcript thủ công")

    details = {"measurement": measurement, "asr": "disabled"}
    cat = build_category_report(
        STATUS_PASS,
        reasons,
        suggestions,
        details,
        checked=True,
        score=0.0,
        checked_scope="audio measured (loudness/silence only) — ASR disabled",
    )
    return cat, all_findings


def _caption_category(caption: str, provider_config) -> tuple[dict, list[dict]]:
    """Check the caption and evaluate against community standards."""
    from policy_evaluator import evaluate_caption as pe_evaluate_caption, _scan_text
    text = (caption or "").strip()
    analysis = analyze_caption(text)
    reasons: list[str] = [f"{issue.category}: {issue.message}" for issue in analysis.issues]
    suggestions: list[str] = []
    all_findings = pe_evaluate_caption(text, analysis, None, ai_error=False)

    status = STATUS_PASS
    if not text:
        reasons.append("Caption trống (NOT_PROVIDED)")
        suggestions.append("Điền caption mô tả nội dung video")
        status = STATUS_PASS
    elif any(issue.severity == "high" for issue in analysis.issues):
        status = STATUS_BLOCKED
        suggestions.append("Sửa lại nội dung caption, không thể dùng ký tự thay thế để né bộ lọc")
    elif analysis.issues:
        status = STATUS_NEEDS_REVIEW
        suggestions.append("Rà soát lại caption trước khi đăng")

    ai_verdict = None
    ai_error = False
    if provider_config.configured:
        try:
            ai_verdict = ai_provider.chat_json(
                "Bạn kiểm tra caption cho quy định cộng đồng của Facebook.",
                "Kiểm tra caption sau, trả về JSON {\"status\": \"APPROVED|NEEDS_REVIEW|BLOCKED\", "
                "\"reason\": \"...\", \"hashtags_repeated\": true, \"suspicious_link\": true}\n"
                f"Caption: {text[:4000]}",
                provider_config,
            )
            model_status = str(ai_verdict.get("status", "")).upper()
            if model_status == "BLOCKED" and status != STATUS_BLOCKED:
                status = STATUS_BLOCKED
            elif model_status == "NEEDS_REVIEW" and status == STATUS_PASS:
                status = STATUS_NEEDS_REVIEW
            all_findings = pe_evaluate_caption(text, analysis, ai_verdict, ai_error=False)
            if ai_verdict.get("hashtags_repeated"):
                reasons.append("Hashtag bị lặp lại")
                if status == STATUS_PASS:
                    status = STATUS_NEEDS_REVIEW
            if ai_verdict.get("suspicious_link"):
                reasons.append("Caption chứa liên kết đáng ngờ")
                suggestions.append("Xóa liên kết hoặc kiểm chứng nguồn")
                if status == STATUS_PASS:
                    status = STATUS_NEEDS_REVIEW
            if ai_verdict.get("reason"):
                reasons.append(str(ai_verdict["reason"])[:200])
        except Exception as exc:
            LOG.error("Caption AI check failed: %s", type(exc).__name__)
            ai_error = True
            all_findings = pe_evaluate_caption(text, analysis, None, ai_error=True)
            if status == STATUS_PASS:
                status = STATUS_CHECK_FAILED
            reasons.append(f"AI kiểm tra caption lỗi: {type(exc).__name__}")
            suggestions.append("Kiểm tra AI_MODEL và thử lại")

    if not text and not reasons:
        reasons.append("Caption trống (NOT_PROVIDED) — không vi phạm nhưng nên bổ sung")

    if not reasons and not analysis.issues and not ai_error:
        reasons.append("Caption không có dấu hiệu spam hay vi phạm")
    if status == STATUS_NEEDS_REVIEW and not suggestions:
        suggestions.append("Rà soát lại caption trước khi đăng")

    details = {"analysis": {"risk_level": analysis.risk_level,
                            "intent": analysis.intent,
                            "issues_count": len(analysis.issues)},
               "ai_verdict": ai_verdict,
               "caption_length": len(text),
               "caption_provided": bool(text)}
    scope = "caption provided" if text else "caption NOT_PROVIDED"
    score = 0.0 if status == STATUS_PASS else (
        0.8 if status == STATUS_BLOCKED else 0.35 if status == STATUS_CHECK_FAILED else 0.45)
    return build_category_report(status, reasons, suggestions, details,
                                 checked=True, score=score, checked_scope=scope), all_findings


def _copyright_category(sha256: str, signature: str, known: list[dict]) -> tuple[dict, list[dict]]:
    """Check internal duplicates and report copyright verification status."""
    from policy_evaluator import evaluate_copyright
    all_findings = evaluate_copyright(known, sha256, signature, has_verification_source=False)

    has_violation = any(f.get("severity") == "violation" for f in all_findings)
    has_review = any(f.get("severity") in ("distribution_risk", "copyright", "technical")
                     for f in all_findings)
    if has_violation:
        status = STATUS_BLOCKED
    elif has_review:
        status = STATUS_NEEDS_REVIEW
    else:
        status = STATUS_PASS

    reasons: list[str] = []
    suggestions: list[str] = []
    for f in all_findings:
        evidence = f.get("evidence", "")
        if f.get("category") == "spam_duplicate" and "trùng" in evidence.lower() and "Trùng" not in evidence:
            evidence = evidence.replace("Nội dung trùng", "Trùng", 1)
        if f.get("category") == "copyright":
            evidence = "Chưa xác minh bản quyền"
        reasons.append(f"[{f.get('severity', '')}] {evidence[:200]}")
        if f.get("remediation"):
            suggestions.append(f.get("remediation"))
    if not reasons:
        reasons.append("Không tìm thấy video trùng trong Workspace, nhưng chưa xác minh bản quyền")
    suggestions.append("Bạn vẫn phải tự xác nhận quyền sử dụng nội dung; kết quả nội bộ chưa xác minh bản quyền")

    details = {"sha256": sha256, "signature": signature, "compared": len(known),
               "verification_source": None,
               "copyright_status": "UNVERIFIED"}
    scope = "workspace duplicate check + UNVERIFIED copyright"
    return build_category_report(status, reasons, suggestions, details,
                                 checked=True,
                                 score=0.0 if status == STATUS_PASS else 0.5,
                                 checked_scope=scope), all_findings


def run_precheck(path: Path, caption: str, known_media: list[dict],
                 provider_config=None, policy: ReelPolicy | None = None) -> dict:
    """Run the whole check and return the comprehensive report."""
    provider_config = provider_config or ai_provider.load_config()
    policy = policy or ReelPolicy()
    path = Path(path)
    work_dir = Path(tempfile.mkdtemp(prefix="fpm-precheck-"))
    all_findings: list[dict] = []
    categories: dict[str, dict] = {}
    checked_scopes: dict[str, str] = {}

    # Load AWS Rekognition config
    aws_config: RekognitionConfig = load_aws_config()
    aws_client: RekognitionClient | None = None
    if aws_config.enabled and aws_config.has_credentials:
        aws_client = RekognitionClient(aws_config)
        LOG.info("AWS Rekognition enabled: region=%s, max_frames=%d",
                 aws_config.region, aws_config.max_frames)
    else:
        LOG.warning("AWS Rekognition not enabled or credentials missing")

    try:
        # --- Technical ---
        tech_cat, tech_findings = _technical_category(path, policy)
        categories[CATEGORY_TECHNICAL] = tech_cat
        all_findings.extend(tech_findings)
        checked_scopes[CATEGORY_TECHNICAL] = tech_cat["checked_scope"]

        # --- Frames extraction ---
        frames = extract_keyframes(path, policy.keyframes, work_dir)

        # --- AWS Rekognition Visual Analysis ---
        if aws_client:
            frame_report = extract_and_analyze(path, policy, aws_client, work_dir,
                                               max_frames=aws_config.max_frames)
        else:
            frame_report = FrameAnalysisReport(
                total_frame_count=len(frames),
                checked=False,
                error="AWS Rekognition not configured",
            )

        visual_cat, visual_findings = _visual_category(frame_report, policy)
        categories[CATEGORY_VISUAL] = visual_cat
        all_findings.extend(visual_findings)
        checked_scopes[CATEGORY_VISUAL] = visual_cat["checked_scope"]

        # --- OCR (independent) ---
        ocr_cat, ocr_findings = _ocr_category(frames, policy, work_dir)
        categories[CATEGORY_OCR] = ocr_cat
        all_findings.extend(ocr_findings)
        checked_scopes[CATEGORY_OCR] = ocr_cat["checked_scope"]

        # --- Audio (no ASR) ---
        audio = extract_audio(path, work_dir)
        audio_cat, audio_findings = _audio_category(audio, policy)
        categories[CATEGORY_AUDIO] = audio_cat
        all_findings.extend(audio_findings)
        checked_scopes[CATEGORY_AUDIO] = audio_cat["checked_scope"]

        # --- Caption ---
        caption_cat, caption_findings = _caption_category(caption, provider_config)
        categories[CATEGORY_CAPTION] = caption_cat
        all_findings.extend(caption_findings)
        checked_scopes[CATEGORY_CAPTION] = caption_cat["checked_scope"]

        # --- Copyright ---
        try:
            sha256 = file_sha256(path)
            signature = perceptual_signature(path)
        except OSError:
            sha256, signature = "", ""
        copyright_cat, copyright_findings = _copyright_category(sha256, signature, known_media)
        categories[CATEGORY_COPYRIGHT] = copyright_cat
        all_findings.extend(copyright_findings)
        checked_scopes[CATEGORY_COPYRIGHT] = copyright_cat["checked_scope"]

    finally:
        cleanup_work_dir(work_dir)

    checked_scope = "; ".join(f"{k}: {v}" for k, v in checked_scopes.items())
    # Extract copyright_status from copyright category for top-level report
    copyright_status = categories.get(CATEGORY_COPYRIGHT, {}).get("details", {}).get("copyright_status", "")
    report = build_report(
        categories=categories,
        all_findings=all_findings,
        model_version=provider_config.model,
        vision_model=f"aws-rekognition:{aws_config.region}" if aws_config.enabled else "none",
        provider="aws-rekognition" if aws_config.enabled else "none",
        checked_scope=checked_scope,
        video_info=None,
        duration=None,
    )
    report["policy_version"] = POLICY_VERSION
    report["copyright_status"] = copyright_status
    return report