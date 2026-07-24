"""Generate Winnow's exact-text social and README graphics."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
FONT_DIR = Path("C:/Windows/Fonts")

INK = "#11130f"
PANEL = "#191c17"
LINE = "#353a30"
PAPER = "#f2f4ed"
MUTED = "#aeb5a6"
GREEN = "#61d685"
AMBER = "#f2b84b"
RED = "#ff766f"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size=size)


def draw_mark(draw: ImageDraw.ImageDraw, x: int, y: int, scale: int = 1) -> None:
    """Draw a compact grain-and-filter mark."""
    draw.rounded_rectangle(
        (x, y, x + 58 * scale, y + 58 * scale),
        radius=8 * scale,
        fill=GREEN,
    )
    stroke = max(2, 3 * scale)
    draw.line(
        (x + 28 * scale, y + 43 * scale, x + 28 * scale, y + 14 * scale),
        fill=INK,
        width=stroke,
    )
    for dx, dy, side in [(0, 0, -1), (0, 9, 1), (0, 18, -1)]:
        cx = x + 28 * scale
        cy = y + (15 + dy) * scale
        tip = cx + side * 12 * scale
        draw.line((cx, cy + 5 * scale, tip, cy), fill=INK, width=stroke)
    draw.line(
        (x + 13 * scale, y + 47 * scale, x + 45 * scale, y + 47 * scale),
        fill=AMBER,
        width=stroke,
    )


def social_preview() -> None:
    img = Image.new("RGB", (1280, 640), INK)
    draw = ImageDraw.Draw(img)

    for x in range(0, 1280, 64):
        draw.line((x, 0, x - 180, 640), fill="#171a15", width=1)

    draw_mark(draw, 72, 64)
    draw.text((148, 62), "WINNOW", font=font("segoeuib.ttf", 48), fill=PAPER)
    draw.text(
        (72, 170),
        "Compress noisy CLI output.",
        font=font("segoeuib.ttf", 49),
        fill=PAPER,
    )
    draw.text(
        (72, 229),
        "Recall every original.",
        font=font("segoeuib.ttf", 49),
        fill=GREEN,
    )
    draw.text(
        (72, 326),
        "A local-first output proxy for Codex, Claude Code, and your shell.",
        font=font("segoeui.ttf", 24),
        fill=MUTED,
    )

    labels = ["ZERO LLM CALLS", "SEARCHABLE RECALL", "WINDOWS + UNIX"]
    x = 72
    for index, label in enumerate(labels):
        color = AMBER if index == 0 else GREEN
        width = draw.textlength(label, font=font("segoeuib.ttf", 17))
        draw.rounded_rectangle(
            (x, 395, x + width + 32, 437),
            radius=7,
            outline=color,
            width=2,
        )
        draw.text((x + 16, 405), label, font=font("segoeuib.ttf", 17), fill=color)
        x += int(width) + 48

    draw.text(
        (72, 532),
        "github.com/Farhanward/winnow",
        font=font("segoeui.ttf", 22),
        fill=PAPER,
    )

    px, py, pw, ph = 810, 62, 398, 516
    draw.rounded_rectangle(
        (px, py, px + pw, py + ph),
        radius=8,
        fill=PANEL,
        outline=LINE,
        width=2,
    )
    draw.ellipse((px + 24, py + 22, px + 36, py + 34), fill=RED)
    draw.ellipse((px + 44, py + 22, px + 56, py + 34), fill=AMBER)
    draw.ellipse((px + 64, py + 22, px + 76, py + 34), fill=GREEN)
    draw.text((px + 24, py + 76), "SYNTHETIC JSON CASE", font=font("segoeuib.ttf", 16), fill=MUTED)
    draw.text((px + 24, py + 119), "42,079", font=font("segoeuib.ttf", 54), fill=PAPER)
    draw.text((px + 234, py + 142), "tokens in", font=font("segoeui.ttf", 20), fill=MUTED)
    draw.line((px + 25, py + 212, px + 373, py + 212), fill=LINE, width=2)
    draw.text((px + 24, py + 245), "347", font=font("segoeuib.ttf", 70), fill=GREEN)
    draw.text((px + 170, py + 280), "tokens out", font=font("segoeui.ttf", 21), fill=MUTED)
    draw.text((px + 24, py + 342), "99.2% smaller", font=font("segoeuib.ttf", 30), fill=AMBER)
    draw.rounded_rectangle(
        (px + 24, py + 414, px + 374, py + 474),
        radius=7,
        fill="#0c0e0b",
        outline=LINE,
        width=1,
    )
    draw.text((px + 43, py + 431), "$ wn recall a1b2c3", font=font("consola.ttf", 19), fill=PAPER)

    img.save(ASSETS / "winnow-social-preview.png", optimize=True)


def demo() -> None:
    img = Image.new("RGB", (1200, 720), "#0d0f0c")
    draw = ImageDraw.Draw(img)

    draw_mark(draw, 48, 38)
    draw.text((124, 43), "Winnow keeps the signal", font=font("segoeuib.ttf", 34), fill=PAPER)
    draw.text(
        (124, 86),
        "The full original stays searchable on your machine.",
        font=font("segoeui.ttf", 20),
        fill=MUTED,
    )

    left = (48, 148, 570, 652)
    right = (630, 148, 1152, 652)
    for box in (left, right):
        draw.rounded_rectangle(box, radius=8, fill=PANEL, outline=LINE, width=2)

    mono = font("consola.ttf", 18)
    mono_bold = font("consolab.ttf", 19)
    draw.text((76, 176), "RAW OUTPUT", font=font("segoeuib.ttf", 16), fill=AMBER)
    draw.text((658, 176), "WINNOW VIEW", font=font("segoeuib.ttf", 16), fill=GREEN)

    raw_lines = [
        "$ npm install",
        "npm warn deprecated package-1...",
        "npm warn deprecated package-2...",
        "npm warn deprecated package-3...",
        "npm warn deprecated package-4...",
        "npm warn deprecated package-5...",
        "npm warn deprecated package-6...",
        "npm warn deprecated package-7...",
        "npm warn deprecated package-8...",
        "npm warn deprecated package-9...",
        "npm warn deprecated package-10...",
        "... 170 more warning lines ...",
        "added 512 packages in 8s",
        "found 0 vulnerabilities",
    ]
    for i, line in enumerate(raw_lines):
        color = PAPER if i in (0, 12, 13) else "#8e9588"
        draw.text((76, 222 + i * 27), line, font=mono, fill=color)

    compact_lines = [
        ("$ wn run -- npm install", PAPER),
        ("", PAPER),
        ("added 512 packages in 8s", PAPER),
        ("found 0 vulnerabilities", GREEN),
        ("", PAPER),
        ("... 180 warning lines hidden", MUTED),
        ("", PAPER),
        ("winnow npm-install", AMBER),
        ("3,060 -> 37 tokens", PAPER),
        ("saved 98.8%", GREEN),
        ("", PAPER),
        ("full: wn recall a1b2c3", PAPER),
    ]
    for i, (line, color) in enumerate(compact_lines):
        draw.text((658, 222 + i * 32), line, font=mono_bold if i in (7, 9) else mono, fill=color)

    img.save(ASSETS / "winnow-demo.png", optimize=True)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    social_preview()
    demo()
    print(f"Generated assets in {ASSETS}")


if __name__ == "__main__":
    main()
