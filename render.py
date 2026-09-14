"""Render an HTML ad to PNG in real Chromium and report message text outside the canvas."""

import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

SIZE = 1080

# Measured in the page after fonts load: message text that leaves the canvas, is clipped,
# or is painted over by another element.
MEASURE = r"""
() => {
  const SIZE = __SIZE__;
  const SHAPES = new Set(['path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'text', 'tspan', 'use']);
  const ownTextNodes = e => [...e.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
  const range = document.createRange();
  const textRects = node => { range.selectNodeContents(node); return [...range.getClientRects()]; };

  // Does element e itself put visible pixels at (x, y)? Containers and texture overlays don't count.
  const paints = (e, x, y) => {
    const cs = getComputedStyle(e);
    if (cs.visibility === 'hidden') return false;
    let alpha = 1;
    for (let a = e; a && a !== document.body; a = a.parentElement) {  // opacity and blend come from ancestors too
      const s = getComputedStyle(a), r = a.getBoundingClientRect();
      alpha *= +s.opacity;
      if (s.mixBlendMode !== 'normal' || (a === e && r.width * r.height > 0.6 * SIZE * SIZE)) return false;
    }
    if (alpha < 0.5) return false;
    if (SHAPES.has(e.tagName.toLowerCase())) return cs.fill !== 'none';  // stroke-only marks are annotation
    const bg = cs.backgroundColor;
    if ((bg && bg !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(bg)) || cs.backgroundImage !== 'none') return true;
    // text-only element: only where its glyphs are (middle band), not its whole line box
    return ownTextNodes(e).some(n => textRects(n).some(l =>
      x >= l.left && x <= l.right && y >= l.top + l.height * 0.25 && y <= l.bottom - l.height * 0.25));
  };

  const out = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (el.closest('[data-bleed]')) continue;  // decorative type is allowed off canvas
    const nodes = ownTextNodes(el);
    const text = nodes.map(n => n.textContent.trim()).join(' ');
    if (!text) continue;
    const label = `"${text.slice(0, 40)}"`;
    if (r.left < -1 || r.top < -1 || r.right > SIZE + 1 || r.bottom > SIZE + 1) {
      out.push(`${label} box ${Math.round(r.left)},${Math.round(r.top)} to ${Math.round(r.right)},${Math.round(r.bottom)}`);
    }
    if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflow === 'hidden') {
      out.push(`${label} is clipped by its container`);
    }
    let hits = 0, samples = 0, cover = null;
    for (const node of nodes) {
      for (const line of textRects(node)) {
        for (const fx of [0.1, 0.3, 0.5, 0.7, 0.9]) {
          const x = line.left + line.width * fx, y = line.top + line.height / 2;
          if (x < 0 || y < 0 || x > SIZE || y > SIZE) continue;
          samples++;
          for (const top of document.elementsFromPoint(x, y)) {  // top of the stack down to our text
            if (el.contains(top) || top.contains(el)) break;
            const a = top.textContent.replace(/\s+/g, ' ').trim(), b = el.textContent.replace(/\s+/g, ' ').trim();
            if (a && b && (a.includes(b) || b.includes(a))) continue;  // offset shadow copy of the same words
            if (paints(top, x, y)) { hits++; cover = top; break; }
          }
        }
      }
    }
    if (samples && hits / samples >= 0.3) {
      const cls = typeof cover.className === 'string' && cover.className ? '.' + cover.className.split(' ')[0] : '';
      out.push(`${label} is covered by another element (<${cover.tagName.toLowerCase()}${cls}>) at ${Math.round(100 * hits / samples)}% of sampled points`);
    }
  }
  return out;
}
""".replace("__SIZE__", str(SIZE))


_lock = threading.Lock()  # one Chromium at a time


def render_png(html: str, png: Path) -> list[str]:
    """Writes png, returns a list of overflow problems (empty means it fits)."""
    with _lock, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": SIZE, "height": SIZE})
        page.set_content(html, wait_until="networkidle", timeout=30000)
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(png), clip={"x": 0, "y": 0, "width": SIZE, "height": SIZE})
        # pointer-events:none would hide overlays from elementFromPoint; turn it off only for measuring
        page.add_style_tag(content="*{pointer-events:auto !important}")
        problems = page.evaluate(MEASURE)
        browser.close()
    return problems
