"""Idea mockups: 2-4 standalone HTML/CSS screens per storyboard, in the IDEA's own look (not Ramblr's theme).
Safety in three layers: the generator is told no scripts or network; sanitize() strips anything active; the site
serves them with a CSP of default-src 'none' inside iframes that have a bare sandbox (no allow-scripts, no
allow-same-origin). PNG thumbnails come from headless Playwright with JavaScript off, so they show on a phone."""
import re
from pathlib import Path

from . import config

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"

PROMPT = """You design MOCKUPS for a product idea. Below is its storyboard. Design 3 key screens (never fewer than 2, never more than 4) as standalone, polished, believable app screens.

HARD RULES:
- Each mockup is ONE complete HTML document with all CSS inline in a <style> tag.
- NO JavaScript of any kind, NO external resources: no <link>, no @import, no web fonts, no http(s) URLs, no <img src> except data: URIs. Use system fonts and inline SVG / CSS shapes for art.
- Fixed canvas: html, body {margin:0}; body is exactly 960px wide and 600px tall, overflow hidden.
- Match THE IDEA'S OWN LOOK (a game gets a game HUD palette and type, a recipe tool gets something warm, a finance tool gets something sober). Do NOT use a dark navy / teal / mint "cyberpunk dashboard" style, that is the host app's theme and must not leak in unless the idea itself calls for it.
- Use realistic placeholder content (names, numbers, labels), not lorem ipsum. Show the screen mid-use, not empty.
- Screens should follow the storyboard panels in order (pick the 2-4 most important moments).

OUTPUT FORMAT, exactly, nothing before or after:
=== MOCKUP 1: short screen title ===
<!doctype html>...
=== END ===
=== MOCKUP 2: short screen title ===
...
=== END ===

STORYBOARD:
"""

BLOCK = re.compile(r"=== MOCKUP\s+\d+\s*:\s*(.+?)\s*===\s*\n(.*?)\n\s*=== END ===", re.S)
_DROP_TAGS = re.compile(r"<\s*(script|iframe|object|embed|link|meta|base|form|frame|frameset|applet|audio|video|source|input|button|textarea|select)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_DROP_SINGLE = re.compile(r"<\s*/?\s*(script|iframe|object|embed|link|meta|base|form|frame|frameset|applet|audio|video|source|input|button|textarea|select)\b[^>]*>", re.I)
_ON_ATTR = re.compile(r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_BAD_URL_ATTR = re.compile(r"""\s(href|src|action|formaction|xlink:href|srcset|poster|background)\s*=\s*("(?!data:image/)[^"]*"|'(?!data:image/)[^']*'|(?!data:image/)[^\s>]+)""", re.I)
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(?!data:image/)[^)]*\)", re.I)
_CSS_IMPORT = re.compile(r"@import[^;]*;?", re.I)
_EXPR = re.compile(r"expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding", re.I)
MAX_BYTES = 40_000


def sanitize(doc):
    d = _DROP_TAGS.sub("", doc)
    d = _DROP_SINGLE.sub("", d)
    d = _ON_ATTR.sub("", d)
    d = _BAD_URL_ATTR.sub("", d)
    d = _CSS_IMPORT.sub("", d)
    d = _CSS_URL.sub("none", d)
    d = _EXPR.sub("", d)
    if len(d.encode()) > MAX_BYTES:
        raise ValueError("mockup too large")
    meta = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
    if re.search(r"<head[^>]*>", d, re.I):
        d = re.sub(r"(<head[^>]*>)", r"\1" + meta, d, count=1, flags=re.I)
    elif re.search(r"<html[^>]*>", d, re.I):
        d = re.sub(r"(<html[^>]*>)", r"\1<head>" + meta + "</head>", d, count=1, flags=re.I)
    else:
        d = f"<!doctype html><html><head>{meta}</head><body>{d}</body></html>"
    return d


def parse(text):
    out = []
    for title, html in BLOCK.findall(text):
        html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html.strip())
        try:
            out.append({"n": len(out) + 1, "title": title.strip()[:80], "html": sanitize(html)})
        except ValueError:
            continue
        if len(out) == 4:
            break
    return out


def thumb_dir():
    p = config.DATA / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def thumb_path(storyboard_id, n):
    return thumb_dir() / f"{storyboard_id}-{n}.png"


def render_thumbs(storyboard_id, mockups):
    """Headless Playwright on the system Chromium, JS off, no network: one 960x600 PNG per mockup."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox", "--disable-gpu"])
        ctx = b.new_context(viewport={"width": 960, "height": 600}, java_script_enabled=False, offline=True)
        for m in mockups:
            pg = ctx.new_page()
            pg.set_content(m["html"], wait_until="load")
            pg.screenshot(path=str(thumb_path(storyboard_id, m["n"])))
            pg.close()
        b.close()
