"""Synthesize a monospaced cut of Base Neue for terminal use.

Keeps the original glyph outlines intact and re-spaces every glyph onto a
uniform advance width (the widest printable-ASCII glyph), optically centering
each outline inside the new cell so nothing clips or overlaps. The result is a
true monospace family ("Base Neue Mono") that terminal emulators accept without
any layout compromise.
"""

import os
import sys

from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "assets", "fonts")
OUT_DIR = os.path.join(SRC_DIR, "mono")

FAMILY = "Base Neue Mono"

WEIGHTS = [
    ("BaseNeue-Light.ttf", 300, "Light", False),
    ("BaseNeue-Regular.ttf", 400, "Regular", False),
    ("BaseNeue-Medium.ttf", 500, "Medium", False),
    ("BaseNeue-SemiBold.ttf", 700, "Bold", True),
]


def target_advance(font: TTFont) -> int:
    cmap = font.getBestCmap()
    return font["hmtx"][cmap[ord("M")]][0]


def monospace(source_path: str, out_path: str, cell: int, weight: int, subfamily: str, bold: bool) -> None:
    font = TTFont(source_path)
    glyph_order = font.getGlyphSet()
    glyf = font["glyf"]
    hmtx = font["hmtx"]
    cmap_best = font.getBestCmap()

    for name in font.getGlyphOrder():
        original_advance, _ = hmtx[name]
        if original_advance == 0 or original_advance == cell:
            continue

        shift = (cell - original_advance) // 2
        pen = TTGlyphPen(glyph_order)
        transform_pen = TransformPen(pen, (1, 0, 0, 1, shift, 0))
        glyph_order[name].draw(transform_pen)
        rebuilt = pen.glyph()
        glyf[name] = rebuilt
        try:
            coordinates, _, _ = rebuilt.getCoordinates(glyf)
            lsb = min(coordinates.x) if len(coordinates) > 0 else 0
        except Exception:
            lsb = getattr(rebuilt, "xMin", 0)
        hmtx[name] = (cell, lsb)

    for codepoint in (0x20,):
        glyph_name = cmap_best.get(codepoint)
        if glyph_name is not None:
            hmtx[glyph_name] = (cell, 0)

    if "GPOS" in font:
        del font["GPOS"]

    post = font["post"]
    post.isFixedPitch = 1

    os2 = font["OS/2"]
    os2.usWeightClass = weight
    fs_selection = os2.fsSelection
    fs_selection &= ~0x01
    fs_selection &= ~0x20
    fs_selection &= ~0x40
    if bold:
        fs_selection |= 0x20
    elif weight == 400:
        fs_selection |= 0x40
    os2.fsSelection = fs_selection
    os2.panose.bProportion = 9

    head = font["head"]
    mac_style = head.macStyle
    mac_style &= ~0x01
    if bold:
        mac_style |= 0x01
    head.macStyle = mac_style

    legacy_family = FAMILY if bold else f"{FAMILY} {subfamily}"
    legacy_subfamily = "Bold" if bold else "Regular"
    full_name = f"{FAMILY} {subfamily}"
    postscript_name = f"BaseNeueMono-{subfamily}"

    name_table = font["name"]
    for name_id, value in (
        (1, legacy_family),
        (2, legacy_subfamily),
        (3, full_name),
        (4, full_name),
        (6, postscript_name),
        (16, FAMILY),
        (17, subfamily),
    ):
        name_table.setName(value, name_id, platformID=3, platEncID=1, langID=0x409)

    font.save(out_path)
    return cell


def verify(path: str) -> None:
    font = TTFont(path)
    hmtx = font["hmtx"]
    advances = {advance for advance, _ in hmtx.metrics.values() if advance > 0}
    assert len(advances) == 1, f"{path}: expected uniform advance, got {sorted(advances)}"
    assert font["post"].isFixedPitch == 1


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    cell = max(target_advance(TTFont(os.path.join(SRC_DIR, source_name))) for source_name, _, _, _ in WEIGHTS)
    for source_name, weight, subfamily, bold in WEIGHTS:
        source_path = os.path.join(SRC_DIR, source_name)
        out_name = source_name.replace("BaseNeue-", "BaseNeueMono-")
        out_path = os.path.join(OUT_DIR, out_name)
        monospace(source_path, out_path, cell, weight, subfamily, bold)
        verify(out_path)
        print(f"built {os.path.relpath(out_path, ROOT)} (weight {weight}, cell {cell}upm)")
    print("all faces verified monospace")
    return 0


if __name__ == "__main__":
    sys.exit(main())
