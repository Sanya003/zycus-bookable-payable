"""ocr.py — using Tesseract.

Runs via subprocess with no API keys or paid vision services. Preprocess at
300 DPI for better accuracy.

Save both outputs:
  .txt — page text for extraction
  .tsv — words, bounding boxes, and confidence for grounding.

Default language: eng. Additional Tesseract languages can be enabled via
--langs and TESSDATA_PREFIX.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def has_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def available_langs() -> list[str]:
    try:
        out = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            check=False,
        )
        langs: list[str] = []
        for line in (out.stdout + out.stderr).splitlines():
            line = line.strip()
            if line and not line.startswith("List of"):
                langs.append(line.split()[0])
        return langs
    except (OSError, subprocess.SubprocessError):
        return []


def ocr_page(png: Path, txt_out: Path, tsv_out: Path, langs: str = "eng") -> dict:
    """OCR one PNG. Returns {text_path, tsv_path, mean_conf}."""
    if not has_tesseract():
        raise RuntimeError("tesseract binary not found (apt: tesseract-ocr)")
    
    txt_out.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)

    # Allow local ./tessdata override without code change.
    local_td = Path("tessdata")
    if local_td.is_dir() and "TESSDATA_PREFIX" not in env:
        env["TESSDATA_PREFIX"] = str(local_td.resolve())

    # Filter requested langs to installed ones; always keep eng fallback.
    installed = set(available_langs())
    want = [lg for lg in langs.split("+") if lg in installed] or ["eng"]
    lang_arg = "+".join(want)

    # Full text.
    subprocess.run(
        ["tesseract", str(png), str(txt_out.with_suffix("")), "-l", lang_arg],
        check=True,
        env=env,
        capture_output=True,
    )
    # TSV word boxes.
    subprocess.run(
        ["tesseract", str(png), str(tsv_out.with_suffix("")), "-l", lang_arg, "tsv"],
        check=True,
        env=env,
        capture_output=True,
    )

    return {
        "text_path": str(txt_out),
        "tsv_path": str(tsv_out),
        "mean_conf": _mean_conf(tsv_out),
        "langs_used": lang_arg,
    }


def _mean_conf(tsv: Path) -> float:
    try:
        lines = tsv.read_text(encoding="utf-8", errors="ignore").splitlines()
        confs = []
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) >= 11:
                try:
                    c = float(parts[10])
                    if c >= 0:
                        confs.append(c)
                except ValueError:
                    pass
        return round(sum(confs) / len(confs), 1) if confs else 0.0
    except FileNotFoundError:
        return 0.0