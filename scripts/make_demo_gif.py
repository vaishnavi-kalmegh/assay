"""Render docs/demo.gif from the REAL output of the commands below.

Each command is actually run (from the repository root) and its output captured.
Only the typing animation is synthetic. Needs Pillow (`pip install pillow`) and the
`assay` command on PATH. Run:  python scripts/make_demo_gif.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "demo.gif"
COMMANDS = [
    "assay map",
    "awk -F'|' '/needs_|no_test_|below_/ {print $2 $3}' reports/mapping/report.md",
    "assay acceptance",
    "assay evaluate mutants",
    "assay compare",
]
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/Menlo.ttc",
    "C:/Windows/Fonts/consola.ttf",
]
WIDTH, FONT_SIZE, LINE, PAD, BAR = 980, 15, 22, 22, 34
BG, FG, DIM = (13, 17, 23), (230, 237, 243), (170, 180, 190)
GREEN, AMBER, BARBG = (63, 185, 80), (210, 153, 34), (30, 36, 44)


def capture() -> list[tuple[str, list[str]]]:
    results = []
    for command in COMMANDS:
        done = subprocess.run(
            command, shell=True, cwd=ROOT, capture_output=True, text=True, check=False
        )
        text = (done.stdout + done.stderr).rstrip("\n")
        results.append((command, [line.rstrip() for line in text.splitlines()]))
    return results


def load_font() -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    sys.exit("no monospace font found; add one to FONT_PATHS")


def main() -> None:
    steps = capture()
    font = load_font()
    total = sum(1 + len(lines) for _, lines in steps) + len(steps) - 1
    height = BAR + PAD * 2 + LINE * total

    def render(shown: list[tuple[str, str]], cursor: bool = False) -> Image.Image:
        image = Image.new("RGB", (WIDTH, height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, WIDTH, BAR], fill=BARBG)
        for index, colour in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            draw.ellipse([16 + index * 22, 11, 28 + index * 22, 23], fill=colour)
        draw.text(
            (96, 8),
            "assay - rendered from the captured output of the real commands",
            font=font,
            fill=DIM,
        )
        y = BAR + PAD
        for kind, text in shown:
            if kind == "cmd":
                draw.text((PAD, y), "$", font=font, fill=GREEN)
                draw.text((PAD + 20, y), text, font=font, fill=FG)
            else:
                colour = AMBER if "_" in text and text.startswith(" REQ") else DIM
                if text.startswith(("A.", "B.", "C.")):
                    colour = FG
                draw.text((PAD, y), text, font=font, fill=colour)
            y += LINE
        if cursor:
            draw.rectangle([PAD + 20, y - LINE + 3, PAD + 30, y - 4], fill=FG)
        return image

    frames: list[Image.Image] = []
    delays: list[int] = []
    shown: list[tuple[str, str]] = []
    for position, (command, lines) in enumerate(steps):
        if position:
            shown.append(("out", ""))
        for end in range(0, len(command) + 1, 3):
            frames.append(render([*shown, ("cmd", command[:end])], cursor=True))
            delays.append(45)
        shown.append(("cmd", command))
        frames.append(render(shown))
        delays.append(450)
        for line in lines:
            shown.append(("out", line))
            frames.append(render(shown))
            delays.append(130)
        delays[-1] = 1100
    delays[-1] = 4000

    palette = [
        frame.quantize(
            colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
        )
        for frame in frames
    ]
    palette[0].save(
        OUTPUT,
        save_all=True,
        append_images=palette[1:],
        duration=delays,
        loop=0,
        optimize=True,
    )
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size // 1024} KiB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
