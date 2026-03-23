"""
Generate launcher/logos.ico from assets/logo.svg.

Run from the repo root before PyInstaller:
    python launcher/make_icon.py

Requires: Pillow (always), cairosvg (optional but preferred for accuracy).
Install:  pip install Pillow cairosvg
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SVG_PATH = REPO_ROOT / "assets" / "logo.svg"
OUT_PATH = Path(__file__).parent / "logos.ico"

# ICO sizes required by Windows (256px is the "shell" size; 48/32/16 for taskbar/menu)
ICO_SIZES = [256, 128, 64, 48, 32, 16]


def svg_to_png_bytes(size: int) -> bytes:
    """Convert the SVG to a PNG byte-string at the given square size."""
    try:
        import cairosvg  # type: ignore
        return cairosvg.svg2png(
            url=str(SVG_PATH),
            output_width=size,
            output_height=size,
        )
    except ImportError:
        return _fallback_png_bytes(size)


def _fallback_png_bytes(size: int) -> bytes:
    """PIL fallback — draws the Logos gradient circle when cairosvg isn't available."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m = max(2, size // 16)  # margin
    # Gradient from indigo (#6366f1) to purple (#a855f7) — top-left to bottom-right
    for i in range(size - 2 * m):
        t = i / max(1, size - 2 * m - 1)
        r = int(0x63 + t * (0xa8 - 0x63))
        g = int(0x66 + t * (0x55 - 0x66))
        b = int(0xf1 + t * (0xf7 - 0xf1))
        draw.arc(
            [m + i, m + i, size - m - i - 1, size - m - i - 1],
            start=0, end=360,
            fill=(r, g, b, 255),
            width=1,
        )
    # Solid filled ellipse over the gradient lines
    for i in range(size - 2 * m):
        t = i / max(1, size - 2 * m - 1)
        r = int(0x63 + t * (0xa8 - 0x63))
        g = int(0x66 + t * (0x55 - 0x66))
        b = int(0xf1 + t * (0xf7 - 0xf1))
        draw.ellipse(
            [m, m, size - m - 1, size - m - 1],
            fill=(r, g, b, int(255 * (1 - abs(t - 0.5) * 0.4))),
        )
    return _img_to_png_bytes(img)


def _img_to_png_bytes(img) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_ico() -> None:
    from PIL import Image

    frames: list[Image.Image] = []
    for size in ICO_SIZES:
        png_bytes = svg_to_png_bytes(size)
        frame = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        frame = frame.resize((size, size), Image.LANCZOS)
        frames.append(frame)

    largest = frames[0]  # 256×256
    largest.save(
        str(OUT_PATH),
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=frames[1:],
    )
    print(f"[make_icon] Saved {OUT_PATH}  ({', '.join(str(s) for s in ICO_SIZES)} px)")


if __name__ == "__main__":
    try:
        build_ico()
    except Exception as exc:
        print(f"[make_icon] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
