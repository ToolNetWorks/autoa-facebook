"""End to end checks for the rebuilt interface.

Run against a preview build or the live service:

    python tests/e2e_ui.py http://127.0.0.1:4173
    python tests/e2e_ui.py http://127.0.0.1:9088

The script drives a real browser, fails on console errors, failed requests,
horizontal overflow, controls that are separated from their label and text
that is squeezed into a vertical column.
"""
import os
import sys

os.environ.setdefault("DISPLAY", ":99")
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9088"
ADMIN = os.environ.get("FPM_ADMIN_PASSWORD", "")
USER = os.environ.get("FPM_ADMIN_USER", "admin")

VIEWPORTS = [(390, 844), (402, 844), (430, 932), (768, 1024), (834, 1112), (1280, 900), (1440, 900)]

SECTIONS = [
    "overview",
    "accounts",
    "album",
    "groups",
    "screening",
    "auto-posting",
    "group-post",
    "reels",
    "ai-caption",
    "video-precheck",
    "safety",
    "schedule",
    "comments",
    "history",
]

LAYOUT_PROBE = """() => {
  const problems = [];
  const overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  if (overflow > 1) problems.push({type: 'horizontal-overflow', px: overflow});
  document.querySelectorAll('input[type=checkbox],input[type=radio],.switch input').forEach(box=>{
    const r = box.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    if (r.width > 32 || r.height > 32) {
      problems.push({type: 'control-too-big', w: Math.round(r.width), h: Math.round(r.height)});
    }
    const label = box.closest('label') || (box.id ? document.querySelector(`label[for="${box.id}"]`) : null);
    if (label) {
      const lr = label.getBoundingClientRect();
      const dx = Math.abs(lr.x - r.x);
      const dy = Math.abs(lr.y - r.y);
      if (dx > 70 || dy > 70) {
        problems.push({type: 'control-far-from-label', dx: Math.round(dx), dy: Math.round(dy)});
      }
    }
  });
  const seen = new Set();
  document.querySelectorAll('main *').forEach(node=>{
    if (node.children.length) return;
    const text = (node.textContent || '').trim();
    if (text.length < 4) return;
    const r = node.getBoundingClientRect();
    if (r.width > 0 && r.height > 70 && r.width < 45) {
      const key = text.slice(0, 24) + Math.round(r.width);
      if (seen.has(key)) return;
      seen.add(key);
      problems.push({type: 'column-text', w: Math.round(r.width), h: Math.round(r.height), text: text.slice(0, 40)});
    }
  });
  const main = document.querySelector('main');
  if (main && main.scrollWidth > main.clientWidth + 2) {
    problems.push({type: 'main-overflow', scroll: main.scrollWidth, client: main.clientWidth});
  }
  return problems;
}"""


def fail(message):
    print(f"FAIL  {message}")
    return 1


def main():
    problems = 0
    console_errors = []
    failed_requests = []

    api_errors = []
    error_toasts = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height in VIEWPORTS:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on("console", lambda m: console_errors.append(f"{width}px {m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(f"{width}px pageerror: {e}"))
            page.on(
                "requestfailed",
                lambda r: failed_requests.append(f"{width}px {r.url} {r.failure}"),
            )
            page.on(
                "response",
                lambda r: api_errors.append(f"{width}px {r.status} {r.request.method} {r.url}")
                if "/api/" in r.url and r.status >= 400 and r.status != 401
                else None,
            )
            page.on(
                "console",
                lambda m: None,
            )

            page.goto(BASE, wait_until="domcontentloaded")
            page.wait_for_selector("#loginForm", timeout=20000)
            page.fill("#loginForm input[name=username]", USER)
            page.fill("#loginForm input[name=password]", ADMIN)
            page.click("#loginForm button[type=submit]")
            page.wait_for_selector(".workspace-card", timeout=30000)
            page.set_default_timeout(4000)
            print(f"OK    {width}px  đăng nhập và tải dashboard")

            if width in (390, 1440):
                page.screenshot(path=f"/tmp/kilo/new-dashboard-{width}.png", full_page=True)

            # Bảng quản trị cũng phải sạch lỗi.
            for tab in ("dashboard", "collaborators", "access-keys", "audit"):
                # Mỗi mục quản trị nằm trên trang Dashboard; hai mục còn lại
                # nhảy vào Workspace nên phải quay lại Dashboard trước.
                page.evaluate("() => { window.location.hash = '#/'; }")
                page.wait_for_timeout(700)
                if width < 1024:
                    opened = "open" in (page.query_selector(".sidebar").get_attribute("class") or "")
                    if not opened:
                        toggle = page.query_selector(".menu-toggle")
                        if toggle and toggle.is_visible():
                            toggle.click()
                            page.wait_for_timeout(500)
                try:
                    page.click(f'.nav-item[data-page="{tab}"]', timeout=2500)
                except Exception:
                    continue
                page.wait_for_timeout(900)
                error_toasts.extend(
                    f"{width}px admin/{tab}: {text}"
                    for text in page.eval_on_selector_all(
                        ".toast.error, .toast.bad", "els=>els.map(e => e.innerText.trim())"
                    )
                )
                found = page.evaluate(LAYOUT_PROBE)
                if found:
                    problems += len(found)
                    print(f"FAIL  {width}px  admin/{tab}")
                    for item in found[:4]:
                        print("        ", item)
                else:
                    print(f"OK    {width}px  admin/{tab}")
            # Các trang báo cáo của Chủ hệ thống.
            for report in ("/analytics/pipeline/1", "/analytics/collaborator/2"):
                page.evaluate(f"() => {{ window.location.hash = '{report}'; }}")
                page.wait_for_timeout(1500)
                error_toasts.extend(
                    f"{width}px {report}: {text}"
                    for text in page.eval_on_selector_all(
                        ".toast.error, .toast.bad", "els=>els.map(e => e.innerText.trim())"
                    )
                )
                found = page.evaluate(LAYOUT_PROBE)
                if found:
                    problems += len(found)
                    print(f"FAIL  {width}px  {report}")
                    for item in found[:4]:
                        print("        ", item)
                else:
                    print(f"OK    {width}px  {report}")

            page.evaluate("() => { window.location.hash = '#/'; }")
            page.wait_for_timeout(900)

            page.get_by_role("button", name="Mở Workspace").first.click()
            page.wait_for_timeout(1500)
            print(f"OK    {width}px  mở Workspace")

            for section in SECTIONS:
                if width < 1024:
                    opened = "open" in (page.query_selector(".sidebar").get_attribute("class") or "")
                    if not opened:
                        toggle = page.query_selector(".menu-toggle")
                        if toggle and toggle.is_visible():
                            toggle.click()
                            page.wait_for_timeout(500)
                try:
                    page.click(f'.nav-item[data-page={section}]', timeout=2500)
                except Exception:
                    print(f"SKIP  {width}px  {section} (không có trong menu)")
                    continue
                page.wait_for_timeout(900)
                if section == "auto-posting":
                    # Modal lịch đăng phải mở được và có đủ các trường.
                    try:
                        page.locator("button.btn", has_text="Lịch đăng").first.click(timeout=2500)
                        page.wait_for_timeout(1200)
                        if page.locator(".modal").count() != 1:
                            problems += 1
                            print(f"FAIL  {width}px  modal Lich dang khong mo")
                        else:
                            print(f"OK    {width}px  modal Lich dang")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                    except Exception:
                        problems += 1
                        print(f"FAIL  {width}px  modal Lich dang")
                found = page.evaluate(LAYOUT_PROBE)
                if found:
                    problems += len(found)
                    print(f"FAIL  {width}px  {section}")
                    for item in found[:4]:
                        print("        ", item)
                else:
                    print(f"OK    {width}px  {section}")
                if width in (390, 1440):
                    page.screenshot(path=f"/tmp/kilo/new-{section}-{width}.png", full_page=True)

            page.close()
        browser.close()

    if error_toasts:
        problems += len(error_toasts)
        print("FAIL  co toast loi")
        for item in error_toasts[:12]:
            print("        ", item)
    else:
        print("OK    khong co toast loi")

    if api_errors:
        problems += len(api_errors)
        print("FAIL  request API that loi")
        for item in api_errors[:12]:
            print("        ", item)
    else:
        print("OK    khong co request API loi (4xx/5xx)")

    real_console = [e for e in console_errors if "401" not in e and "favicon" not in e]
    if real_console:
        problems += len(real_console)
        print("FAIL  console errors")
        for item in real_console[:10]:
            print("        ", item)
    else:
        print("OK    không có console error")

    if failed_requests:
        problems += len(failed_requests)
        print("FAIL  request thất bại")
        for item in failed_requests[:10]:
            print("        ", item)
    else:
        print("OK    không có request thất bại")

    print()
    print("TONG SO VAN DE:", problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
