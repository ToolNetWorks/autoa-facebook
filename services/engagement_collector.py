"""Thu thập chỉ số tương tác của các bài đã đăng.

Hai nguồn, theo đúng thứ tự ưu tiên:

1. **Facebook Graph API** (chính thức) khi tài khoản có token và bài đã có
   post id. Đây là nguồn đáng tin nhất.
2. **Phiên Facebook** của tài khoản đăng bài: đọc những con số Facebook hiện
   công khai trên trang bài (lượt thích, bình luận, chia sẻ, lượt xem).

Nguyên tắc bất di bất dịch: chỉ số nào không đọc được thì giữ là ``None``
(chưa có dữ liệu). Không bao giờ ghi 0 cho một chỉ số chưa biết, không ước
lượng reach hay lượt xem, và không coi một lượt bấm đăng là đã có tương tác.

Bộ thu thập không vượt CAPTCHA, không đụng checkpoint và không thu thập dữ
liệu riêng tư: nó chỉ đọc những gì người dùng đã đăng và những gì Facebook
hiển thị công khai cho chính tài khoản đó.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GRAPH_VERSION = os.getenv("FB_GRAPH_VERSION", "v21.0")
GRAPH_TOKEN = (os.getenv("FB_GRAPH_TOKEN") or "").strip()
BROWSER_COLLECTOR = (os.getenv("ANALYTICS_BROWSER_COLLECTOR", "1") or "1").strip() not in ("0", "false", "no")
INTERVAL_MINUTES = max(15, int(os.getenv("ANALYTICS_INTERVAL_MINUTES", "180") or 180))
BATCH_SIZE = max(1, int(os.getenv("ANALYTICS_BATCH_SIZE", "5") or 5))

SOURCE_GRAPH = "graph_api"
SOURCE_BROWSER = "facebook_session"
SOURCE_NONE = ""

STATUS_MEASURED = "measured"
STATUS_UNAVAILABLE = "unavailable"
STATUS_REMOVED = "removed"
STATUS_ERROR = "error"

# --- the counters Facebook shows on the page of a post -------------------------
# --- the counters Facebook shows on the page of a post -------------------------
# --- the counters Facebook shows on the page of a post -------------------------
NUMBER = r"([0-9][0-9.,]*)(?:\s*([KkMm]))?"
LABELS = {
    "reactions": r"reactions?|likes?|lượt thích|thích",
    "comments": r"comments?|bình luận",
    "shares": r"shares?|lượt chia sẻ|chia sẻ",
    "views": r"views?|lượt xem",
}


def scale_count(raw: str, suffix: str = "") -> int | None:
    """Read "1.2K", "3,4K", "12 345" as a number."""
    if not raw:
        return None
    text = raw.strip().replace(" ", "").replace(".", ",")
    if not text:
        return None
    if "," in text:
        head, _, tail = text.partition(",")
        text = head + ("." + tail if len(tail) in (1, 2) else "")
    try:
        number = int(round(float(text)))
    except ValueError:
        return None
    if (suffix or "").lower() == "k":
        return number * 1000
    if (suffix or "").lower() == "m":
        return number * 1000000
    return number


def counters_from_text(text: str) -> dict:
    """The counters of a post page.

    Facebook writes the number before or after the label depending on the
    language, so every number goes to the label that is closest to it. A
    counter that is not on the page keeps the value None: it means "chưa có
    dữ liệu", never zero.
    """
    content = text or ""
    marks = []
    for metric, pattern in LABELS.items():
        for match in re.finditer(rf"({pattern})", content, re.I):
            marks.append((match.start(), match.end(), metric))
    marks.sort()
    numbers = [(match.start(), match.end(), match.group(1), match.group(2) or "")
               for match in re.finditer(NUMBER, content)]
    candidates = []
    for index, (start, end, _metric) in enumerate(marks):
        previous_end = marks[index - 1][1] if index else 0
        next_start = marks[index + 1][0] if index + 1 < len(marks) else len(content)
        for number_start, number_end, raw, suffix in numbers:
            if number_end <= previous_end or number_start >= next_start:
                continue
            if number_start >= end:
                distance = number_start - end
            elif number_end <= start:
                distance = start - number_end
            else:
                continue
            if distance > 14:
                continue
            candidates.append((distance, index, number_start, raw, suffix))
    candidates.sort()
    result = {metric: None for metric in LABELS}
    taken_labels = set()
    taken_numbers = set()
    for _distance, index, number_start, raw, suffix in candidates:
        metric = marks[index][2]
        if metric in taken_labels or number_start in taken_numbers:
            continue
        value = scale_count(raw, suffix)
        if value is None:
            continue
        result[metric] = value
        taken_labels.add(metric)
        taken_numbers.add(number_start)
    return result


def graph_configured() -> bool:
    """True when an official token is available."""
    return bool(GRAPH_TOKEN)


def browser_configured() -> bool:
    """True when the session reader may be used."""
    return BROWSER_COLLECTOR


def collect_graph(permalink: str, post_id: str) -> dict:
    """Read the insights of a post with the official API.

    Without a token the source stays switched off and the metric keeps the
    state "chưa có dữ liệu".
    """
    empty = {"reactions": None, "comments": None, "shares": None, "views": None,
             "reach": None, "source": SOURCE_NONE, "verification_status": STATUS_UNAVAILABLE,
             "error_code": ""}
    if not graph_configured() or not permalink:
        empty["error_code"] = "graph_not_configured" if not graph_configured() else "no_permalink"
        return empty
    try:
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode({
            "fields": "id,permalink_url,shares,reactions.summary(true).limit(0).total_count,"
                      "comments.summary(true).limit(0).total_count",
            "access_token": GRAPH_TOKEN,
        })
        url = f"{permalink}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "facebook-pipeline-manager/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b"{}")
    except Exception as error:
        return {**empty, "source": SOURCE_GRAPH, "verification_status": STATUS_ERROR,
                "error_code": type(error).__name__.lower()[:40]}
    reactions = payload.get("reactions", {}).get("total_count")
    comments = payload.get("comments", {}).get("total_count")
    shares = payload.get("shares", {}).get("total_count")
    return {
        "reactions": int(reactions) if reactions is not None else None,
        "comments": int(comments) if comments is not None else None,
        "shares": int(shares) if shares is not None else None,
        "views": None,
        "reach": None,
        "source": SOURCE_GRAPH,
        "verification_status": STATUS_MEASURED,
        "error_code": "",
    }


def collect_browser(account_id: int, permalink: str) -> dict:
    """Read the counters the session of the account can see.

    The reader runs as a separate process, so a failure of Facebook can never
    disturb the queue of the posting worker.
    """
    empty = {"reactions": None, "comments": None, "shares": None, "views": None,
             "reach": None, "source": SOURCE_NONE, "verification_status": STATUS_UNAVAILABLE,
             "error_code": ""}
    if not browser_configured() or not permalink or not account_id:
        empty["error_code"] = ("browser_disabled" if not browser_configured()
                               else ("no_permalink" if not permalink else "no_account"))
        return empty
    payload_file = ROOT / "data" / f"analytics-request-{os.getpid()}-{account_id}.json"
    result_file = ROOT / "data" / f"analytics-result-{os.getpid()}-{account_id}.json"
    try:
        payload_file.write_text(json.dumps({"account_id": int(account_id),
                                            "permalink": permalink}), encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "fb_browser.py"), "engagement",
                        str(payload_file), str(result_file)],
                       cwd=ROOT, capture_output=True, text=True, timeout=180)
        if not result_file.exists():
            return {**empty, "source": SOURCE_BROWSER, "verification_status": STATUS_ERROR,
                    "error_code": "no_result"}
        outcome = json.loads(result_file.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        return {**empty, "source": SOURCE_BROWSER, "verification_status": STATUS_ERROR,
                "error_code": "timeout"}
    except Exception as error:
        return {**empty, "source": SOURCE_BROWSER, "verification_status": STATUS_ERROR,
                "error_code": type(error).__name__.lower()[:40]}
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
    status = outcome.get("verification_status", STATUS_UNAVAILABLE)
    counters = outcome.get("counters") or {}
    if status == STATUS_REMOVED:
        return {**empty, "source": SOURCE_BROWSER, "verification_status": STATUS_REMOVED,
                "error_code": outcome.get("error_code", "")}
    if status != STATUS_MEASURED:
        return {**empty, "source": SOURCE_BROWSER, "verification_status": status,
                "error_code": outcome.get("error_code", "")}
    return {
        "reactions": counters.get("reactions"),
        "comments": counters.get("comments"),
        "shares": counters.get("shares"),
        "views": counters.get("views"),
        "reach": None,
        "source": SOURCE_BROWSER,
        "verification_status": STATUS_MEASURED,
        "error_code": "",
    }


def collect_one(account_id: int | None, permalink: str, post_id: str) -> dict:
    """Try every source in order and keep the first measurement."""
    if not permalink:
        return {"reactions": None, "comments": None, "shares": None, "views": None,
                "reach": None, "source": SOURCE_NONE, "verification_status": STATUS_UNAVAILABLE,
                "error_code": "no_permalink"}
    outcome = collect_graph(permalink, str(post_id))
    if outcome.get("verification_status") == STATUS_MEASURED:
        return outcome
    if account_id:
        fallback = collect_browser(int(account_id), permalink)
        if fallback.get("verification_status") in (STATUS_MEASURED, STATUS_REMOVED):
            return fallback
        if outcome.get("error_code") in ("", "no_permalink"):
            outcome["error_code"] = fallback.get("error_code", "")
    return outcome
