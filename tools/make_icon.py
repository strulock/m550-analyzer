"""
Draw the app's logo: a gauge with a redline and a needle on a dark tile.

Writes src/n63read/data/n63read.ico (16-256 px, for the exes, the installer
and the window) and n63read.png (256 px, for Tk and the README). Each size is
drawn on its own, 4x oversampled, with strokes kept thick enough to read at
16 px. Needs Pillow, only here: the app itself doesn't.

    python tools/make_icon.py
"""

import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, "src", "n63read", "data")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
OVERSAMPLE = 4

TOP, BOTTOM = (24, 62, 110), (8, 22, 42)       # tile gradient
DIAL = (232, 240, 250)
REDLINE = (228, 49, 43)
NEEDLE = (255, 138, 31)
HUB = (232, 240, 250)

START, END = 150, 390      # degrees, clockwise from 3 o'clock: 240 deg over the top
RED_FROM = 340             # the redline segment
NEEDLE_AT = 312            # where the needle points


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def draw(size):
    n = size * OVERSAMPLE
    s = n / 256.0                                   # design units: a 256 px tile
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    # Tile: rounded square with a vertical gradient.
    grad = Image.new("RGBA", (n, n))
    g = ImageDraw.Draw(grad)
    for y in range(n):
        t = y / max(1, n - 1)
        g.line([(0, y), (n, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    mask = Image.new("L", (n, n), 0)
    pad = 6 * s if size >= 32 else 0
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, n - 1 - pad, n - 1 - pad],
                                           radius=56 * s, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    cx, cy = 128 * s, 150 * s                     # the open dial, centred
    r = 84 * s
    # Small sizes get relatively thicker strokes.
    k = 1.0 if size >= 48 else 1.35 if size >= 24 else 1.6
    w = round(18 * s * k)
    box = [cx - r, cy - r, cx + r, cy + r]
    d.arc(box, START, RED_FROM, fill=DIAL, width=w)
    d.arc(box, RED_FROM, END, fill=REDLINE, width=w)
    for deg, colour in ((START, DIAL), (END, REDLINE)):         # round ends
        ex, ey = polar(cx, cy, r - w / 2, deg)
        d.ellipse([ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2], fill=colour)

    # Tick marks inside the dial, where there's room for them.
    if size >= 48:
        for i in range(9):
            deg = START + (END - START) * i / 8
            a = polar(cx, cy, r - w - 4 * s, deg)
            b = polar(cx, cy, r - w - 20 * s, deg)
            d.line([a, b], fill=REDLINE if deg >= RED_FROM else DIAL, width=round(7 * s))

    # Needle and hub.
    tip = polar(cx, cy, r - w * 0.4, NEEDLE_AT)
    d.line([(cx, cy), tip], fill=NEEDLE, width=round(14 * s * k))
    hub = 20 * s * k
    d.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=HUB)
    dot = hub * 0.45
    d.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=NEEDLE)

    return img.resize((size, size), Image.LANCZOS)


def main():
    images = {size: draw(size) for size in SIZES}
    ico = os.path.join(OUT, "n63read.ico")
    images[256].save(ico, format="ICO", sizes=[(z, z) for z in SIZES],
                     append_images=[images[z] for z in SIZES if z != 256])
    png = os.path.join(OUT, "n63read.png")
    images[256].save(png, optimize=True)
    print("wrote", os.path.normpath(ico), "and", os.path.normpath(png))


if __name__ == "__main__":
    main()
