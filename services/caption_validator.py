"""Local analysis of Vietnamese captions.

This module is the backend's own authority. It never tries to slip content
past a filter: it detects wording that is abusive, discriminatory, adult,
violent, deceptive, spammy or over-promising, and it reports the problem so a
person can fix the substance. When the wording itself is acceptable it only
tidies up formatting and repetition, and never rewrites meaning.

The same analysis runs again on the rewritten caption, so a provider cannot
quietly reintroduce a problem it claimed to have removed.
"""
from __future__ import annotations

import re
import unicodedata

from schemas.ai_caption import (
    RISK_HIGH, RISK_LOW, RISK_MEDIUM, RISK_UNKNOWN, SEVERITY_HIGH, SEVERITY_LOW,
    SEVERITY_MEDIUM, CaptionAnalysis, CaptionChange, CaptionIssue,
)

# --- term lists -------------------------------------------------------------
# Abuse and harassment.
ABUSIVE_TERMS = (
    "vcl", "vkl", "vc", "cặc", "cl", "lồn", "lồn cục", "đéo", "địt", "cút đi",
    "cút", "biến đi", "mẹ mày", "mẹ mày mày", "óc chó", "chó má", "ngu như",
    "não rỗng", "thằng ngu", "con ngu", "ngu vcl", "súc vật", "thằng chó",
)
# Targeted attacks and demeaning statements.
DISCRIMINATION_TERMS = (
    "đừng có làm người", "không xứng đáng làm người", "bạn xấu xa", "loại vô dụng",
    "đồ vô dụng", "kệ mẹ cả đời", "cái thằng", "con đầy",
)
ADULT_TERMS = (
    "nude", "nudes", "xxx", "18+", "khoả thân", "kính mời vào phòng", "mẹo lắm",
    "gợi ý riêng cho bạn", "dụ để khiêu dỗi", "gái lạ",
)
VIOLENCE_TERMS = (
    "giết chết", "bắn chết", "đâm chết", "đánh chết", "bắt đâm", "đe dọa",
    "đe dọa sẽ", "cho đáng sống",
)
# Deceptive or illegal offers.
SCAM_TERMS = (
    "kiếm tiền online", "kiếm tiền nhanh", "kiếm tiền siêu tốc", "đầu tư siêu lợi nhuận",
    "lãi suất cao nhất", "nhận tiền cùng", "click link", "bấm vào link", "link nhận thưởng",
    "vay tiền online", "casino", "cá bộ", "đánh bạc online", "lô xiên", "mở app nhận quà",
    "gửi mật khẩu", "không cần xác minh", "chuyển khoản trước", "đặt cọc trước",
)
# Claims that cannot be substantiated.
MISLEADING_TERMS = (
    "100% hiệu quả", "chắc chắn thắng", "cam kết hoàn tiền", "không rủi ro",
    "yên tâm tuyệt đối", "chính hãng 100%", "đảm bảo có hiệu quả", "chắc chắn có",
    "không bao giờ thất bại", "tuyệt đối an toàn", "lợi nhuận cao nhất", "thu nhập không giới hạn",
)
# Commercial content that Facebook restricts in some surfaces.
COMMERCIAL_TERMS = (
    "mua ngay", "nhắn tin để đặt hàng", "inbox để đặt hàng", "add zalo để mua",
    "liên hệ ngay để đặt hàng", "ship cod toàn quốc", "giảm giá sập sàn",
)
# Spam behaviour.
SPAM_BEHAVIOUR = (
    "add mình", "follow mình", "nhắn tin cho mình", "inbox ngay", "bấm like đi",
    "tag bạn bè vào", "chia sẻ bài viết này", "đăng ký ngay để nhận",
    "nhấn link", "comment từ khóa",
    "telegram", "zalo", "whatsapp", "messenger",
)

HIGH_SEVERITY_CATEGORIES = ("abusive", "discrimination", "adult", "violence", "scam", "misleading")

PHONE_PATTERN = re.compile(r"(?:\+?84|0)\s?\d{9,10}\b")
URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
HASHTAG_PATTERN = re.compile(r"#\w+", re.UNICODE)
NUMBER_PATTERN = re.compile(
    r"\d[\d.,]*\s*(?:%|k|tr|triệu|vnd|kg|g|ml|l|cm|mm|km|m|tiếng|tờ|trang|người|ngày|"
    r"tháng|năm|tuần|giờ|phút|lần|tấn|tuổi|hộp|chai|thùng|sản phẩm|chuyến)?", re.IGNORECASE)
HANDLE_PATTERN = re.compile(r"@[A-Za-z0-9_.\-]{3,}", re.UNICODE)
COMPACT_NUMBER_PATTERN = re.compile(r"(?<![0-9])\d+(?:k|tr)\d*(?![0-9])", re.IGNORECASE)
CONTACT_CHANNEL = ("telegram", "zalo", "whatsapp", "messenger", "gmail", "email", "hotline", "sms",
                   "inbox", "nhắn tin", "gọi điện", "gọi mình", "quét mã")
MIN_RETENTION_RATIO = 0.35
REPEAT_CHAR_PATTERN = re.compile(r"(.)\1{3,}")
REPEAT_WORD_PATTERN = re.compile(r"\b(\w+)(\s+\1\b)+", re.IGNORECASE)
EXCESS_PUNCT_PATTERN = re.compile(r"([!?.])\1{2,}")
SHOUT_PATTERN = re.compile(r"\b[A-ZÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘỜỚỞỠỢÙÚỦŨỤÝỲÝỶỴỢ]{6,}\b")

MAX_HASHTAGS = 12
MAX_REPEAT_RUN = 3


def normalize(text: str) -> str:
    """Collapse whitespace without touching the words themselves."""
    return re.sub(r"[ \t ]+", " ", (text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def _contains_term(text: str, term: str) -> bool:
    """Word-boundary aware match that also works for multi-word Vietnamese terms."""
    pattern = re.escape(term).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", text, re.IGNORECASE) is not None


def _excerpt(text: str, index: int, length: int = 60) -> str:
    start = max(0, index - 20)
    return text[start:start + length].strip()


def key_facts(text: str) -> list[str]:
    """Numbers, measurements, links and contacts that must survive a rewrite."""
    facts: list[str] = []
    for match in URL_PATTERN.finditer(text or ""):
        facts.append(match.group(0).strip())
    for match in PHONE_PATTERN.finditer(text or ""):
        facts.append(re.sub(r"\s+", "", match.group(0)))
    for match in COMPACT_NUMBER_PATTERN.finditer(text or ""):
        facts.append(match.group(0).strip())
    remainder = COMPACT_NUMBER_PATTERN.sub(" ", text or "")
    for match in NUMBER_PATTERN.finditer(remainder):
        token = re.sub(r"\s+", " ", match.group(0)).strip()
        if not token or not any(char.isdigit() for char in token):
            continue
        digits = re.sub(r"\D", "", token)
        # A bare single digit is noise, a measurement or a longer number is a fact.
        if len(digits) < 2 and len(token) < 3:
            continue
        facts.append(token)
    for match in HANDLE_PATTERN.finditer(text or ""):
        facts.append(match.group(0))
    unique: list[str] = []
    for fact in facts:
        if fact and fact not in unique:
            unique.append(fact)
    return unique


def _fold(value: str) -> str:
    return (value or "").casefold()


def numeric_signature(fact: str) -> str:
    """Canonical value of a numeric fact.

    Vietnamese writes numbers in a compact way, 2k3 means 23 and 5k means 5000,
    so the string form cannot be compared directly.
    """
    text = re.sub(r"\s+", " ", (fact or "").casefold()).strip()
    compact = re.sub(r"\s", "", text)
    match = re.fullmatch(r"(\d+)k(\d+)", compact)
    if match:
        return str(int(match.group(1) + match.group(2)))
    match = re.fullmatch(r"(\d+)tr(\d*)", compact)
    if match:
        fraction = int("0." + match.group(2)) if match.group(2) else 0
        return str(int((int(match.group(1)) + fraction) * 1_000_000))
    number = re.search(r"\d[\d.,]*", text)
    if not number:
        return text
    raw = number.group(0).replace(".", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return text
    if "tr" in compact:
        value *= 1_000_000
    elif re.search(r"\d+k$", compact):
        value *= 1000
    return str(int(value)) if value == int(value) else str(value)


def fact_values(text: str) -> set[tuple[str, str]]:
    """The comparable values of a caption: numbers by value, the rest as text."""
    values: set[tuple[str, str]] = set()
    for fact in key_facts(text):
        if fact.startswith("@") or "://" in fact:
            values.add(("text", fact.casefold()))
        elif re.search(r"\d", fact):
            values.add(("number", numeric_signature(fact)))
        else:
            values.add(("text", fact.casefold()))
    return values


def _content_words(text: str) -> list[str]:
    return [word.casefold() for word in re.findall(r"[\wÀ-ỹ]{2,}", text or "")]


def invented_contact_details(original: str, optimized: str) -> list[str]:
    """Contact channels that the rewrite introduced on its own."""
    source = _fold(original)
    found: list[str] = []
    for channel in CONTACT_CHANNEL:
        if channel in _fold(optimized) and channel not in source:
            found.append(channel)
    for pattern, label in ((PHONE_PATTERN, "số điện thoại"), (URL_PATTERN, "liên kết"),
                            (HANDLE_PATTERN, "tên định danh")):
        for match in pattern.finditer(optimized or ""):
            value = match.group(0).strip()
            if value and value.casefold() not in source:
                found.append(f"{label} {value}")
    return found


def meaning_preserved(original: str, optimized: str) -> bool:
    """True when the rewrite still carries the original meaning.

    Three conditions have to hold: every key fact survives, the rewrite did not
    drop the body of the caption, and it did not introduce a new contact detail
    that was never mentioned by the operator.
    """
    available = fact_values(optimized)
    for kind, value in fact_values(original):
        if (kind, value) not in available:
            return False
    original_words = _content_words(original)
    if original_words:
        optimized_words = set(_content_words(optimized))
        kept = sum(1 for word in original_words if word in optimized_words)
        if kept / len(original_words) < MIN_RETENTION_RATIO:
            return False
    if invented_contact_details(original, optimized):
        return False
    return True


def detect_topic(text: str) -> str:
    """A short, non-invented topic label taken from the caption itself."""
    stripped = normalize(text)
    if not stripped:
        return ""
    words = [word for word in re.split(r"[^\wÀ-ỹ]+", stripped) if len(word) > 2]
    return " ".join(words[:6])


def detect_intent(text: str) -> str:
    lowered = f" {normalize(text).lower()} "
    if any(marker in lowered for marker in ("mua ", "giá", "sản phẩm", "shop", "bán ", "khuyến mãi", "sale")):
        return "quảng bá sản phẩm"
    if any(marker in lowered for marker in ("sự kiện", "ra mắt", "khai trương", "lễ", "hội nghị", "webinar")):
        return "thông báo sự kiện"
    if any(marker in lowered for marker in ("hướng dẫn", "cách ", "mẹo ", "bí quyết", "tutorial", "bước ")):
        return "hướng dẫn"
    if any(marker in lowered for marker in ("cảm ơn", "cảm ơn cộng đồng", "chúc mừng", "xin chào")):
        return "giao tiếp cộng đồng"
    if any(marker in lowered for marker in ("nhận xét", "ý kiến", "góp ý", "bình luận")):
        return "gợi ý thảo luận"
    return "chia sẻ thông tin"


def analyze_caption(text: str) -> CaptionAnalysis:
    """Find policy, meaning and spam problems in a caption."""
    raw = text or ""
    lowered = raw.lower()
    issues: list[CaptionIssue] = []

    def add(category: str, severity: str, message: str, index: int = 0) -> None:
        issues.append(CaptionIssue(category=category, severity=severity, message=message,
                                   excerpt=_excerpt(raw, index) if index else ""))

    for term in ABUSIVE_TERMS:
        if _contains_term(lowered, term):
            add("abusive", SEVERITY_HIGH, f"Ngôn từ công kích hoặc quấy rối: “{term}”. Cần đổi cách diễn đạt thực chất.", lowered.find(term))
            break
    for term in DISCRIMINATION_TERMS:
        if _contains_term(lowered, term):
            add("discrimination", SEVERITY_HIGH, f"Nội dung công kích hoặc phân biệt đối xử: “{term}”.", lowered.find(term))
            break
    for term in ADULT_TERMS:
        if _contains_term(lowered, term):
            add("adult", SEVERITY_HIGH, f"Nội dung không phù hợp hoặc nhạy cảm: “{term}”.", lowered.find(term))
            break
    for term in VIOLENCE_TERMS:
        if _contains_term(lowered, term):
            add("violence", SEVERITY_HIGH, f"Nội dung bạo lực hoặc đe dọa: “{term}”.", lowered.find(term))
            break
    for term in SCAM_TERMS:
        if _contains_term(lowered, term):
            add("scam", SEVERITY_HIGH, f"Dấu hiệu lừa đảo hoặc hoạt động bị hạn chế: “{term}”.", lowered.find(term))
            break
    for term in MISLEADING_TERMS:
        if _contains_term(lowered, term):
            add("misleading", SEVERITY_HIGH, f"Cam kết hoặc lời hứa gây hiểu nhầm: “{term}”. Cần nền tảng căn cứ.", lowered.find(term))
            break
    for term in COMMERCIAL_TERMS:
        if _contains_term(lowered, term):
            add("commercial", SEVERITY_MEDIUM, f"Nội dung thương mại có thể bị hạn chế trên một số bề mặt: “{term}”.", lowered.find(term))
            break
    for term in SPAM_BEHAVIOUR:
        if _contains_term(lowered, term):
            add("spam", SEVERITY_LOW, f"Thủ pháp spam: “{term}”. Nên bỏ lời kêu gọi hành động hàng loạt.", lowered.find(term))
            break

    hashtags = HASHTAG_PATTERN.findall(raw)
    if len(hashtags) > MAX_HASHTAGS:
        add("spam", SEVERITY_LOW, f"Quá nhiều hashtag ({len(hashtags)}). Giữ tối đa {MAX_HASHTAGS}.", 0)
    if REPEAT_WORD_PATTERN.search(raw):
        add("spam", SEVERITY_LOW, "Có từ lặp liên tiếp, đọc rất máy móc.", 0)
    if EXCESS_PUNCT_PATTERN.search(raw):
        add("format", SEVERITY_LOW, "Dấu câu lặp quá nhiều, tạo cảm giác spam.", 0)
    if REPEAT_CHAR_PATTERN.search(raw):
        add("format", SEVERITY_LOW, "Có ký tự lặp quá nhiều.", 0)
    if SHOUT_PATTERN.search(raw):
        add("format", SEVERITY_LOW, "Có đoạn viết hoa toàn bộ, khó đọc và trông như quảng cáo.", 0)
    if PHONE_PATTERN.search(raw):
        add("spam", SEVERITY_LOW, "Có số điện thoại trong nội dung, dễ bị coi là spam hoặc lừa đảo.", 0)
    if len(raw) > 3000:
        add("format", SEVERITY_LOW, "Caption quá dài, nên rút gọn để người đọc dễ nắm ý.", 0)

    risk = RISK_UNKNOWN
    if any(issue.severity == SEVERITY_HIGH for issue in issues):
        risk = RISK_HIGH
    elif any(issue.severity == SEVERITY_MEDIUM for issue in issues):
        risk = RISK_MEDIUM
    elif issues:
        risk = RISK_LOW
    else:
        risk = RISK_LOW

    reason = "Không phát hiện vấn đề về chính sách hay spam." if not issues else \
        "Phát hiện " + str(len(issues)) + " vấn đề cần xử lý."
    return CaptionAnalysis(topic=detect_topic(raw), intent=detect_intent(raw),
                           key_facts=key_facts(raw), issues=issues, risk_level=risk, reason=reason)


def _split_hashtags(text: str) -> tuple[str, list[str]]:
    hashtags = [tag for tag in HASHTAG_PATTERN.findall(text)]
    body = HASHTAG_PATTERN.sub("", text)
    return normalize(body), hashtags


def _dedupe_hashtags(hashtags: list[str], limit: int) -> list[str]:
    seen: list[str] = []
    for tag in hashtags:
        key = tag.lower()
        if key not in [item.lower() for item in seen]:
            seen.append(tag)
        if len(seen) >= limit:
            break
    return seen


def is_shouting(text: str) -> bool:
    """True when the caption is written in capitals throughout."""
    letters = [char for char in (text or "") if char.isalpha()]
    if len(letters) < 12:
        return bool(SHOUT_PATTERN.search(text or ""))
    upper = sum(1 for char in letters if char.isupper())
    return upper / len(letters) > 0.6 or bool(SHOUT_PATTERN.search(text or ""))


def _fix_shouting(text: str) -> tuple[str, bool]:
    """Turn a fully capitalised run into sentence case."""
    if not is_shouting(text):
        return text, False
    result = []
    for word in re.split(r"(\s+)", text):
        stripped = word.strip()
        if len(stripped) >= 3 and stripped.isupper() and stripped.isalpha():
            result.append(word.lower().capitalize())
        else:
            result.append(word)
    return "".join(result), True


def _fix_punctuation(text: str) -> tuple[str, bool]:
    changed = False
    result = EXCESS_PUNCT_PATTERN.sub(lambda match: match.group(1) * 2, text)
    changed = changed or result != text
    result = REPEAT_CHAR_PATTERN.sub(lambda match: match.group(1) * MAX_REPEAT_RUN, result)
    changed = changed or result != text
    result = REPEAT_WORD_PATTERN.sub(lambda match: match.group(1), result)
    changed = changed or result != text
    return result, changed


def _tidy_hashtags(text: str, limit: int) -> tuple[str, bool]:
    body, hashtags = _split_hashtags(text)
    kept = _dedupe_hashtags(hashtags, limit)
    changed = len(kept) != len(hashtags)
    if not kept:
        return normalize(body), changed
    return normalize(body) + " " + " ".join(kept), changed


def _pick_hashtags(text: str, limit: int) -> list[str]:
    """Hashtags built from the caption's own words, never invented topics."""
    words = re.findall(r"[\wÀ-ỹ]{4,}", (text or "").lower())
    stop = {"với", "của", "cho", "các", "những", "một", "được", "này", "đó", "là", "và",
            "trong", "ngoài", "rất", "đã", "sẽ", "không", "còn", "bạn", "mình", "chúng",
            "tôi", "bạn", "nhé", "ạ", "ạy", "cùng", "theo", "sau", "trước", "này"}
    picked: list[str] = []
    for word in words:
        if word in stop:
            continue
        tag = "#" + unicodedata.normalize("NFC", word).replace(" ", "")
        if tag not in picked:
            picked.append(tag)
        if len(picked) >= limit:
            break
    return picked


def rewrite_locally(text: str, *, add_hashtags: bool = False, max_hashtags: int = 5,
                    target: str = "group", tone: str = "") -> tuple[str, list[CaptionChange]]:
    """Tidy a caption without changing its meaning.

    Only formatting is touched: spacing, shouting, repeated characters and
    repeated words, plus optional hashtags drawn from the caption itself. No
    word is added, removed or rephrased, so the meaning cannot drift.
    """
    original = text or ""
    changes: list[CaptionChange] = []
    current = normalize(original)
    if current != original:
        changes.append(CaptionChange(kind="formatting", summary="Chuẩn hóa khoảng trắng và xuống dòng.",
                                     before=original[:2000], after=current[:2000]))

    shouted, did = _fix_shouting(current)
    if did:
        current = shouted
        changes.append(CaptionChange(kind="casing", summary="Đổi chữ in hoa liên tục thành câu thường.",
                                     before=original[:2000], after=current[:2000]))

    punctuated, did = _fix_punctuation(current)
    if did:
        current = punctuated
        changes.append(CaptionChange(kind="punctuation", summary="Giảm dấu câu và từ lặp lộp xộp.",
                                     before=current[:2000], after=punctuated[:2000]))
        current = punctuated

    tidied, did = _tidy_hashtags(current, max_hashtags)
    if did:
        current = tidied
        changes.append(CaptionChange(kind="hashtag", summary=f"Rút gọn hashtag còn tối đa {max_hashtags}.",
                                     before=current[:2000], after=tidied[:2000]))

    if add_hashtags:
        body, existing = _split_hashtags(current)
        room = max(0, max_hashtags - len(existing))
        extra = _pick_hashtags(body, room)
        if extra:
            current = normalize(body) + " " + " ".join(existing + extra)
            changes.append(CaptionChange(kind="hashtag", summary="Thêm hashtag lấy từ chính nội dung: " + " ".join(extra),
                                         before=current[:2000], after=current[:2000]))

    if target == "reel" and len(current) > 400:
        changes.append(CaptionChange(kind="note", summary="Caption Reels dài, giữ phần đầu để người xem đọc nhanh.",
                                     before=current[:2000], after=current[:2000]))
    if tone:
        changes.append(CaptionChange(kind="note", summary=f"Văn phong mong muốn: {tone}. Giữ nguyên ý gốc.",
                                     before="", after=""))
    return current.strip(), changes
