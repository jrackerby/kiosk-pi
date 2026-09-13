#!/usr/bin/env python3
"""Derive every brand raster from the one source artwork.

`source/logo-source.jpg` is the artwork as supplied. This script turns its
off-white page into real transparency and cuts the two crops at the sizes
each consumer wants. Running it is optional -- every file it writes is
committed. It needs `pillow`.

    python3 custom_components/kiosk_pi/brand/build.py
"""
from __future__ import annotations

import os
from collections import deque

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "source", "logo-source.jpg")

# The page is NOT white: it is a warm off-white around (240, 239, 235), with
# JPEG noise taking single channels down to ~234. A threshold tight enough to
# leave the white board inside the house alone is still well clear of it.
BG = 226
PAGE = (240, 239, 235)  # what the rim was blended against -- NOT white
CORE_LUM = 190        # a pixel this dark is solid ink and can stand in as one


def _lum(p):
    return 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]


def key_out_page(src: str) -> Image.Image:
    """Off-white page -> alpha, with the mark left exactly as drawn.

    Flooded from the border ONLY. The whites this mark draws on purpose -- the
    board, its ports, the trace nodes -- sit inside a closed blue outline, so
    the flood never reaches them, and there is no enclosed-white pass: here
    every enclosed white IS ink.
    """
    im = Image.open(src).convert("RGBA")
    W, H = im.size
    px = im.load()
    alpha = bytearray(b"\xff" * (W * H))

    def light(p):
        return p[0] >= BG and p[1] >= BG and p[2] >= BG

    seen = bytearray(W * H)
    q = deque()
    for x in range(W):
        for y in (0, H - 1):
            if light(px[x, y]) and not seen[y * W + x]:
                seen[y * W + x] = 1
                q.append((x, y))
    for y in range(H):
        for x in (0, W - 1):
            if light(px[x, y]) and not seen[y * W + x]:
                seen[y * W + x] = 1
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < W and 0 <= ny < H and not seen[ny * W + nx] \
                    and light(px[nx, ny]):
                seen[ny * W + nx] = 1
                q.append((nx, ny))
    for i, v in enumerate(seen):
        if v:
            alpha[i] = 0

    # The RIM: every pixel antialiased against the page is still opaque and
    # part page-coloured, a halo on anything dark. A luminance gate does not
    # find it -- a half-blue, half-page pixel is darker than any "light" cut
    # and was left standing as a pale outline on the first pass -- so EVERY
    # opaque pixel within two of transparency gets its coverage solved, from
    # the nearest solid ink and against the PAGE colour, not white. Solid ink
    # solves to ~1 and is unchanged by it.
    core = {(x, y) for y in range(H) for x in range(W)
            if alpha[y * W + x] and _lum(px[x, y]) < CORE_LUM}
    resolved = 0
    for y in range(H):
        for x in range(W):
            i = y * W + x
            if not alpha[i]:
                continue
            if not any(0 <= x + dx < W and 0 <= y + dy < H
                       and alpha[(y + dy) * W + x + dx] == 0
                       for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)):
                continue
            c = None
            for r in (1, 2, 3):
                near = [(x + dx, y + dy) for dx in range(-r, r + 1)
                        for dy in range(-r, r + 1) if (x + dx, y + dy) in core]
                if near:
                    c = px[min(near, key=lambda n: (n[0] - x) ** 2 + (n[1] - y) ** 2)]
                    break
            resolved += 1
            if c is None:
                alpha[i] = 0
                continue
            p = px[x, y]
            num = sum((PAGE[k] - p[k]) * (PAGE[k] - c[k]) for k in range(3))
            den = sum((PAGE[k] - c[k]) ** 2 for k in range(3)) or 1
            px[x, y] = (c[0], c[1], c[2], 0)
            alpha[i] = int(round(255 * max(0.0, min(1.0, num / den))))

    for y in range(H):
        for x in range(W):
            p = px[x, y]
            px[x, y] = (p[0], p[1], p[2], alpha[y * W + x])
    print(f"  matte rim pixels resolved: {resolved}")
    return im


def _bands(im: Image.Image) -> list[tuple[int, int]]:
    """Rows with no ink at all -- the gap between the mark and the wordmark."""
    W, H = im.size
    px = im.load()
    rows = [any(px[x, y][3] for x in range(W)) for y in range(H)]
    out, start = [], None
    for y, v in enumerate(rows):
        if not v and start is None:
            start = y
        if v and start is not None:
            if y - start > 3:
                out.append((start, y - 1))
            start = None
    return out


def square(im: Image.Image, pad: float = 1.10) -> Image.Image:
    """Trim to the ink, then centre it in a square with a little air."""
    box = im.crop(im.getbbox())
    w, h = box.size
    side = int(max(w, h) * pad)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(box, ((side - w) // 2, (side - h) // 2))
    return out


def main() -> None:
    print("keying", os.path.relpath(SOURCE, HERE))
    full = key_out_page(SOURCE)
    bands = _bands(full)
    print("  empty row bands:", bands)
    # The mark sits above the widest empty band; the wordmark below it.
    # Measured off the keyed image rather than pinned, so a re-supplied source
    # with different margins still cuts in the right place.
    interior = [b for b in bands if b[0] > 0 and b[1] < full.height - 1]
    assert interior, "no gap between mark and wordmark"
    mark_bottom = max(interior, key=lambda b: b[1] - b[0])[0]

    # THE ICON IS THE MARK ALONE. HACS and the sidebar draw it at 256px and
    # smaller, where a wordmark is grey mush.
    mark = square(full.crop((0, 0, full.width, mark_bottom)))
    lockup = full.crop(full.getbbox())
    # The wordmark is dark grey and vanishes on a dark ground -- and HACS
    # renders the README inside a frontend that is dark by default. The dark
    # variant lifts every wordmark pixel to a light grey and leaves the mark
    # untouched: the rows below the gap are the wordmark, nothing else is.
    dark = lockup.copy()
    dpx = dark.load()
    split = mark_bottom - full.getbbox()[1]
    for y in range(split, dark.height):
        for x in range(dark.width):
            r, g, b, a = dpx[x, y]
            if a:
                dpx[x, y] = (236, 236, 236, a)

    for im, name, w in ((mark, "icon.png", 256),         # the HACS brands check reads this path
                        (mark, "icon@2x.png", 512),
                        (lockup, "logo.png", None),      # the README, light
                        (lockup, "logo@2x.png", None),
                        (dark, "dark_logo.png", None),   # the README, dark (the fallback)
                        (dark, "dark_logo@2x.png", None)):
        if w:
            out = im.resize((w, w), Image.LANCZOS)
        else:
            h = 1024 if "@2x" in name else 512
            out = im.resize((round(im.width * h / im.height), h), Image.LANCZOS)
        path = os.path.join(HERE, name)
        out.save(path, optimize=True)
        assert out.getbbox(), f"{name} rendered empty"
        print(f"  wrote {name:14} {out.size}")


if __name__ == "__main__":
    main()
