"""Draws the Modpack Doctor icon (an original design) and writes modpack_doctor/data/icon.png
(used for the window and taskbar icon). Needs Pillow; run: python tools/make_icon.py"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024  # drawn large, then scaled down for smooth edges
ROOT = Path(__file__).resolve().parent.parent


def shade(c, f):
    return tuple(max(0, min(255, int(v * f))) for v in c)


def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((24, 24, S - 24, S - 24), radius=210, fill=(31, 41, 55))
    # isometric block (a plain cube, three faces)
    cx, cy, w, h = 470, 470, 300, 175
    green = (74, 190, 110)
    top = [(cx, cy - h), (cx + w * 0.87, cy - h * 0.5), (cx, cy), (cx - w * 0.87, cy - h * 0.5)]
    left = [(cx - w * 0.87, cy - h * 0.5), (cx, cy), (cx, cy + w), (cx - w * 0.87, cy + w - h * 0.5)]
    right = [(cx + w * 0.87, cy - h * 0.5), (cx, cy), (cx, cy + w), (cx + w * 0.87, cy + w - h * 0.5)]
    d.polygon(left, fill=shade(green, 0.62))
    d.polygon(right, fill=shade(green, 0.82))
    d.polygon(top, fill=shade(green, 1.15))
    for poly in (top, left, right):
        d.line(poly + [poly[0]], fill=(20, 28, 38), width=14, joint="curve")
    # a seam across the faces: this block is made of "mods"
    d.line([(cx, cy), (cx, cy + w)], fill=(20, 28, 38), width=14)
    # check badge (health check)
    bx, by, br = 730, 735, 215
    d.ellipse((bx - br - 26, by - br - 26, bx + br + 26, by + br + 26), fill=(31, 41, 55))
    d.ellipse((bx - br, by - br, bx + br, by + br), fill=(255, 255, 255))
    d.ellipse((bx - br + 34, by - br + 34, bx + br - 34, by + br - 34), fill=(34, 160, 84))
    pts = [(bx - 98, by + 6), (bx - 28, by + 80), (bx + 105, by - 75)]
    d.line(pts, fill=(255, 255, 255), width=50, joint="curve")
    for x, y in (pts[0], pts[2]):
        d.ellipse((x - 25, y - 25, x + 25, y + 25), fill=(255, 255, 255))
    return im


if __name__ == "__main__":
    big = draw()
    png = big.resize((256, 256), Image.LANCZOS)
    (ROOT / "modpack_doctor" / "data").mkdir(exist_ok=True)
    png.save(ROOT / "modpack_doctor" / "data" / "icon.png")
    print("icon written")
