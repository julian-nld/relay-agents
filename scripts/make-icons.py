"""Render Relay's app icons (PNG) from its SVG logo, for phones' home screens.

    PYTHONPATH=vendor-dev python3 scripts/make-icons.py

iOS and Android round the corners themselves, so the PNGs fill the square;
the maskable one keeps the logo inside the middle 60% (Android crops it).
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parent.parent / "src" / "relay" / "static"
# The "R", moved so its strokes sit in the middle of the 64×64 box.
GLYPH = ('<path transform="translate(3 -4)" d="M18 22h14a8 8 0 0 1 0 16h-6l12 12M18 22v28" fill="none" '
         'stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>')


def svg(scale: float) -> str:
    # The glyph is drawn for a 64×64 box; scale it around the centre.
    offset = 32 * (1 - scale)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" '
            f'fill="#10a37f"/><g transform="translate({offset} {offset}) scale({scale})">{GLYPH}</g></svg>')


ICONS = [("icon-180.png", 180, 1.0), ("icon-192.png", 192, 1.0), ("icon-512.png", 512, 1.0),
         ("icon-maskable-512.png", 512, 0.72)]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    for name, size, scale in ICONS:
        page.set_viewport_size({"width": size, "height": size})
        page.set_content('<body style="margin:0">'
                         f'{svg(scale).replace("<svg ", f"<svg width={size} height={size} ")}</body>')
        page.screenshot(path=str(STATIC / name), omit_background=True)
        print("wrote", name)
    browser.close()
