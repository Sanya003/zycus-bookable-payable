"""render.py — PDF page -> PNG image (no text extraction, just pixels).

'pdftoppm' first and 'PyMuPDF' fallback because:
  These PDFs are scans, so we never trust embedded text. 'pdftoppm' renders
  exactly what a human sees. 'PyMuPDF' is the backup when poppler is missing
  (e.g. inside a slim container). Both are free + offline.

Design decision: render at 300 DPI. 150 is fast but tesseract misreads
"7" vs "1" and "," vs "." — and one misread decimal breaks booking to the
cent. 300 costs disk (~1MB/page) but lives in gitignored work/, not output/.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def has_pdftoppm() -> bool:
    return shutil.which("pdftoppm") is not None


def page_count(pdf: Path) -> int:
    """Free, fast page count via PyMuPDF (no rendering)."""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        return len(doc)


def render_pdf(pdf: Path, out_dir: Path, dpi: int = 300) -> list[Path]:
    """Render all pages of pdf to out_dir/stem-*.png. Returns sorted PNGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf.stem
    # Skip re-render if already there.
    existing = sorted(out_dir.glob(f"{stem}-*.png"))
    if existing:
        return existing
    if has_pdftoppm():
        prefix = str(out_dir / stem)
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), str(pdf), prefix],
            check=True,
        )
        return sorted(out_dir.glob(f"{stem}-*.png"))

    # Fallback: PyMuPDF rendering.
    import pymupdf

    paths: list[Path] = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi)
            p = out_dir / f"{stem}-{i}.png"
            pix.save(str(p))
            paths.append(p)
    return paths