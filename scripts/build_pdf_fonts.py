"""Merge the bundled web subsets into complete fonts for PDF embedding."""
from pathlib import Path

from fontTools.merge import Merger

root = Path(__file__).resolve().parents[1]
target = root / "data" / "fonts"
target.mkdir(exist_ok=True)
for weight in (400, 700):
    paths = [str(root / "frontend" / "public" / "fonts" / f"pt-sans-{subset}-{weight}-normal.woff2")
             for subset in ("latin", "latin-ext", "cyrillic", "cyrillic-ext")]
    font = Merger().merge(paths)
    font.flavor = None
    font.save(target / f"PTSans-{weight}.ttf")
    print(f"PT Sans {weight}: {len(font.getBestCmap())} characters")
