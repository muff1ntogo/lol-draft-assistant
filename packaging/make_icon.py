"""Draws the app icon: packaging/app.ico (the programs, the window, the installer) and web/public/favicon.png.

    uv run --with pillow python packaging/make_icon.py

A dark rounded tile with a gold hexagon ring and two chevrons facing each other, blue and red: the two sides of a
draft. Drawn large and scaled down, so the small sizes stay crisp. The colours are the app's (web/src/app.css).
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
S = 1024  # drawn at this size
BG_TOP, BG_BOTTOM = (31, 38, 48), (13, 17, 23)
GOLD, BLUE, RED = (227, 179, 65), (76, 154, 255), (232, 90, 90)
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def hexagon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(math.radians(60 * i - 90)), cy + r * math.sin(math.radians(60 * i - 90))) for i in range(6)]


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # tile: vertical gradient, rounded corners
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    c = S / 2
    # gold hexagon ring, with a soft glow under it
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(hexagon(c, c, S * 0.40), outline=GOLD + (140,), width=int(S * 0.07))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.03)))
    d.polygon(hexagon(c, c, S * 0.40), outline=GOLD + (255,), width=int(S * 0.055))

    # two chevrons facing each other across a gap, "> <": blue from the left, red from the right
    gap, w, h, t = S * 0.045, S * 0.12, S * 0.14, S * 0.07
    for colour, sign in ((BLUE, -1), (RED, 1)):
        tip = (c + sign * gap, c)
        back_x = c + sign * (gap + w)
        d.line([(back_x, c - h), tip, (back_x, c + h)], fill=colour + (255,), width=int(t), joint="curve")
    return img


def main() -> None:
    big = draw()
    ico = ROOT / "packaging" / "app.ico"
    big.resize((256, 256), Image.LANCZOS).save(ico, sizes=[(s, s) for s in SIZES])
    fav = ROOT / "web" / "public" / "favicon.png"
    fav.parent.mkdir(parents=True, exist_ok=True)
    big.resize((64, 64), Image.LANCZOS).save(fav)
    big.resize((512, 512), Image.LANCZOS).save(ROOT / "packaging" / "app-512.png")
    print(f"wrote {ico}, {fav}")


if __name__ == "__main__":
    main()
