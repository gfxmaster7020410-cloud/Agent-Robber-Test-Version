from pathlib import Path
import re
import shutil

src_svg = Path(r"D:\Users\SAIF ALI KHAN\Downloads\SVG")
src_fonts = Path(r"D:\Users\SAIF ALI KHAN\Downloads\Fonts")
root = Path(r"H:\The Agent Robber UI Design Sample\CLI Version\gui")
brand = root / "public" / "brand"
fonts_bn = root / "public" / "fonts" / "base-neue"
fonts_bg = root / "public" / "fonts" / "bogita-mono"

for d in (brand, fonts_bn, fonts_bg):
    d.mkdir(parents=True, exist_ok=True)

named = [
    "Background.svg",
    "horizental line.svg",
    "dual horizental line.svg",
    "Vertical lines.svg",
    "Logo.svg",
    "Text enterance field.svg",
    "Accept.svg",
    "revert.svg",
    "Undo.svg",
    "Redo.svg",
    "Asset 31.svg",
    "collapsed.svg",
    "expanded.svg",
    "Collapse icon for side bar.svg",
    "icon for side bar.svg",
]
for name in named:
    src = src_svg / name
    if not src.is_file():
        print("MISSING", name)
        continue
    dest_name = (
        name.replace("horizental", "horizontal")
        .replace("enterance", "entrance")
        .replace(" ", "-")
        .replace("Asset-31.svg", "slash-mark.svg")
    )
    if name == "Asset 31.svg":
        dest_name = "slash-mark.svg"
    if name == "Collapse icon for side bar.svg":
        dest_name = "sidebar-collapse.svg"
    if name == "icon for side bar.svg":
        dest_name = "sidebar-expand.svg
    shutil.copy2(src, brand / dest_name)
    print("copied", dest_name, src.stat().st_size)

shutil.copy2(src_fonts / "Base Nueu" / "BaseNeue-Regular.ttf", fonts_bn / "BaseNeue-Regular.ttf")
shutil.copy2(src_fonts / "Base Nueu" / "BaseNeue-Medium.ttf", fonts_bn / "BaseNeue-Medium.ttf")
shutil.copy2(src_fonts / "Base Nueu" / "BaseNeue-SemiBold.ttf", fonts_bn / "BaseNeue-SemiBold.ttf")
shutil.copy2(
    src_fonts / "Bogita Mono" / "BogitaMono-Regular-BF6911ee47db964.otf",
    fonts_bg / "BogitaMono-Regular.otf",
)
shutil.copy2(
    src_fonts / "Bogita Mono" / "BogitaMono-Medium-BF6911ee493b6c4.otf",
    fonts_bg / "BogitaMono-Medium.otf",
)
shutil.copy2(
    src_fonts / "Bogita Mono" / "BogitaMono-SemiBold-BF6911ee495cf50.otf",
    fonts_bg / "BogitaMono-SemiBold.otf",
)

bg = (src_svg / "Background.svg").read_text(encoding="utf-8", errors="replace")
print("BG viewBox", re.search(r'viewBox="[^"]+"', bg))
print("BG linear", bg.count("linearGradient"), "radial", bg.count("radialGradient"), "image", bg.count("<image"))
print("BG start")
print(bg[:1800])
