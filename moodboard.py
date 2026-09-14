"""Pinterest moodboard in headless Chromium, no login.

Signed-out search pages still load the first rows of pins behind a sign-in
modal. Instead of screenshotting the modal, collect the pin image URLs and lay
them out on our own contact sheet, which is what the art director agent reads.
Pins are style reference only; nothing from them goes into an ad.
"""

from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from render import _lock

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

COLLECT = """
() => {
  const seen = new Set(), out = [];
  for (const img of document.querySelectorAll('img[src*="i.pinimg.com"]')) {
    if (img.naturalWidth < 150) continue;
    const src = img.src.replace(/\\/(236x|170x|60x60|75x75_RS)\\//, '/474x/');
    if (seen.has(src)) continue;
    seen.add(src);
    const a = img.closest('a[href*="/pin/"]');
    out.push({src, pin: a ? a.href : null, alt: (img.alt || '').slice(0, 120)});
  }
  return out;
}
"""


def sheet_html(query, pins):
    cells = "".join(f'<div><img src="{p["src"]}"></div>' for p in pins)
    return f"""<html><body style="margin:0;background:#111;font-family:sans-serif">
<div style="color:#fff;font-size:28px;padding:18px 24px">Pinterest: {query}</div>
<div style="columns:4;column-gap:12px;padding:0 12px">{cells}</div>
<style>div>div{{break-inside:avoid;margin-bottom:12px}} img{{width:100%;display:block;border-radius:10px}}</style>
</body></html>"""


def pinterest_moodboard(queries: list[str], out_dir: Path, per_query: int = 12) -> list[dict]:
    """Returns [{query, sheet, pins:[{src,pin,alt}]}]. Queries that load nothing are skipped."""
    out_dir.mkdir(parents=True, exist_ok=True)
    boards = []
    with _lock, sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 1600}, user_agent=UA)
        page = ctx.new_page()
        for n, q in enumerate(queries, 1):
            try:
                page.goto(f"https://www.pinterest.com/search/pins/?q={quote(q)}",
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(5000)
                pins = page.evaluate(COLLECT)[:per_query]
            except Exception:
                pins = []
            if len(pins) < 4:
                continue
            sheet = out_dir / f"board_{n}.jpg"
            page.set_content(sheet_html(q, pins), wait_until="networkidle", timeout=45000)
            page.screenshot(path=str(sheet), type="jpeg", quality=80, full_page=True)
            boards.append({"query": q, "sheet": f"moodboard/{sheet.name}", "pins": pins})
        browser.close()
    return boards
