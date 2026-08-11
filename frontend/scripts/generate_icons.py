"""Render the PWA icon set from the brand mark.

    uv run --with pillow python frontend/scripts/generate_icons.py

Writes PNGs into `frontend/public/`, which Vite copies verbatim into
`static/dist/`. Committed as binaries, so this script is the only record of how
they were produced — regenerate here rather than editing the PNGs.

The geometry is the favicon in `frontend/index.html`, reproduced exactly: an
installed icon that does not match the browser-tab icon reads as a different
app. If the favicon changes, change these constants to match and re-run.

Three variants, because they are masked differently:
  icon-{192,512}.png     `purpose: any` — the icon is shown as drawn, so it
                         carries its own rounded corners.
  icon-maskable-512.png  `purpose: maskable` — Android crops this to its own
                         shape (circle, squircle, …). Only the centre 80%
                         diameter is guaranteed visible, so the background runs
                         edge to edge and the mark is scaled well inside it.
  apple-touch-icon.png   iOS applies its own rounded-rect mask and composites
                         any transparency onto black. Square and fully opaque.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

# ── Brand mark, in the favicon's 32×32 coordinate space ──────────────────────
VIEWBOX = 32.0
BG = "#0d0d0d"
CORNER_R = 7.0  # favicon's rx

EDGE_COLOR = "#4f9cf9"
EDGE_WIDTH = 1.5
EDGES = [
    ((10, 16), (16, 9)),
    ((10, 16), (16, 23)),
    ((16, 9), (22, 13)),
    ((16, 23), (22, 19)),
]
# (cx, cy, r, fill)
NODES = [
    (10, 16, 3.0, "#4f9cf9"),
    (16, 9, 2.5, "#22c55e"),
    (16, 23, 2.5, "#22c55e"),
    (22, 13, 2.0, "#a78bfa"),
    (22, 19, 2.0, "#a78bfa"),
]

SS = 8  # supersample factor; the mark is all curves and diagonals


def _render(size: int, *, corner_r: float, mark_scale: float) -> Image.Image:
    """Draw the mark at `size`px.

    `mark_scale` shrinks the mark about the centre without shrinking the
    background — that is the whole mechanism behind the maskable safe zone.
    """
    canvas = size * SS
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle(
        [0, 0, canvas - 1, canvas - 1],
        radius=corner_r / VIEWBOX * canvas,
        fill=BG,
    )

    unit = canvas / VIEWBOX
    centre = VIEWBOX / 2

    def pt(x: float, y: float) -> tuple[float, float]:
        return (
            (centre + (x - centre) * mark_scale) * unit,
            (centre + (y - centre) * mark_scale) * unit,
        )

    stroke = EDGE_WIDTH * mark_scale * unit
    for (x1, y1), (x2, y2) in EDGES:
        a, b = pt(x1, y1), pt(x2, y2)
        d.line([a, b], fill=EDGE_COLOR, width=round(stroke))
        # PIL strokes have butt caps; the favicon uses stroke-linecap="round",
        # so cap each end with a dot of the same diameter.
        for cx, cy in (a, b):
            r = stroke / 2
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=EDGE_COLOR)

    for cx, cy, r, fill in NODES:
        x, y = pt(cx, cy)
        rr = r * mark_scale * unit
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=fill)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = pathlib.Path(__file__).resolve().parent.parent / "public"
    out.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, int]] = []

    def save(img: Image.Image, name: str) -> None:
        path = out / name
        img.save(path, "PNG", optimize=True)
        written.append((name, path.stat().st_size))

    for size in (192, 512):
        save(_render(size, corner_r=CORNER_R, mark_scale=1.0), f"icon-{size}.png")

    # The safe zone is a circle of 80% diameter, i.e. radius 0.40 of the icon.
    # The mark's own circumscribed radius is ~0.398 at scale 1.0 (half-diagonal
    # of its 17×19 bounding box about the centre) — right on the limit. 0.80
    # brings that to ~0.318: clear margin for Android's varying crops, without
    # shrinking the mark to a speck the way a more timid factor does.
    save(_render(512, corner_r=0.0, mark_scale=0.80), "icon-maskable-512.png")

    # Square + opaque: iOS masks it itself and composites alpha onto black.
    apple = Image.new("RGB", (180, 180), BG)
    apple.paste(_render(180, corner_r=0.0, mark_scale=1.0), (0, 0))
    save(apple, "apple-touch-icon.png")

    for name, size in written:
        print(f"{name:26} {size:>7,} bytes")


if __name__ == "__main__":
    main()
