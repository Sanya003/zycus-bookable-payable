"""CLI pipeline: documents/ -> work/ and output/.

python -m src.run documents output --stage output  
     
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from erp import erp_book

from .classify import file_decision
from .extract import extract_file
from .match import Masters
from .ocr import _mean_conf, available_langs, ocr_page
from .render import page_count, render_pdf


def verify_payable(payable: dict[str, Any]) -> dict[str, Any]:
    """Compare one draft's printed gross with the sealed ERP recompute."""
    want_raw = payable.get("gross_total", "")
    try:
        want = float(want_raw) if want_raw not in ("", None) else None
    except (TypeError, ValueError):
        want = None
    try:
        booked = erp_book(payable)
    except (TypeError, ValueError) as exc:
        return {"will_book_gross": None, "currency": payable.get("currency", ""),
                "want": want_raw, "match": False, "delta": None, "error": str(exc)}
    will = booked["will_book_gross"]
    match = want is not None and abs(will - want) < 0.015
    return {"will_book_gross": will, "currency": booked["currency"],
            "want": want_raw, "match": match,
            "delta": round(will - want, 2) if want is not None else None}


def write_output(drafts: list[dict[str, Any]], out_dir: Path) -> list[Path]:
    """Write only the public per-file output contract, without diagnostics."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for draft in drafts:
        clean = {"file": draft["file"], "payables": draft.get("payables", []),
                 "declined": draft.get("declined", [])}
        path = out_dir / f"{Path(clean['file']).stem}.json"
        path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        written.append(path)
    return written


def process_one(pdf: Path, work: Path, dpi: int, langs: str, refresh: bool = False) -> dict:
    pages_dir = work / "pages"
    ocr_dir = work / "ocr"
    pages_dir.mkdir(parents=True, exist_ok=True)
    ocr_dir.mkdir(parents=True, exist_ok=True)
    if refresh:
        for path in pages_dir.glob(f"{pdf.stem}-*.png"):
            path.unlink()
        for suffix in (".txt", ".tsv"):
            for path in ocr_dir.glob(f"{pdf.stem}-*{suffix}"):
                path.unlink()
    pngs = render_pdf(pdf, pages_dir, dpi=dpi)
    pages = []
    for png in pngs:
        txt = ocr_dir / f"{png.stem}.txt"
        tsv = ocr_dir / f"{png.stem}.tsv"
        if not txt.exists() or not tsv.exists():
            info = ocr_page(png, txt, tsv, langs=langs)
        else:
            info = {
                "text_path": str(txt),
                "tsv_path": str(tsv),
                "mean_conf": _mean_conf(tsv),
                "langs_used": langs,
            }
        text = txt.read_text(encoding="utf-8", errors="ignore") if txt.exists() else ""
        pages.append(
            {
                "page": png.stem,
                "png": str(png),
                "mean_conf": info["mean_conf"],
                "chars": len(text),
                "preview": text[:300].replace("\n", " | "),
            }
        )
    return {
        "file": pdf.name,
        "pages": len(pngs),
        "langs_requested": langs,
        "langs_installed": available_langs(),
        "page_detail": pages,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Process PDF documents into autodrafts")
    ap.add_argument("input_dir", help="folder of PDFs, e.g. documents")
    ap.add_argument("work_dir", help="working folder, e.g. work")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--langs", default="eng")
    ap.add_argument("--refresh", action="store_true",
                    help="discard cached pages/OCR and rerun at the selected DPI")
    ap.add_argument("--limit", type=int, default=0, help="process only first N PDFs")
    ap.add_argument("--stage", default="all",
                    choices=["ocr", "classify", "extract", "build", "output", "all"])
    ap.add_argument("--output-dir", default="output", help="graded output folder (for --stage output/all)")
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(in_dir.glob("*.pdf"))

    if args.limit:
        pdfs = pdfs[: args.limit]
    print(f"PDFs: {len(pdfs)} | langs installed: {available_langs()}")

    manifest = []
    if args.stage in ("ocr", "all"):
        for pdf in pdfs:
            try:
                n = page_count(pdf)
            except (OSError, RuntimeError, ValueError) as e:
                print(f"SKIP {pdf.name}: page_count failed: {e}")
                continue
            print(f"... {pdf.name} ({n}p)")
            manifest.append(process_one(pdf, work, args.dpi, args.langs, args.refresh))
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Wrote {work / 'manifest.json'}")
        for m in manifest:
            avg = (
                round(sum(p["mean_conf"] for p in m["page_detail"]) / len(m["page_detail"]), 1)
                if m["page_detail"]
                else 0.0
            )
            print(f"  {m['file']}: {m['pages']}p avg_conf={avg}")

    if args.stage in ("classify", "extract", "build", "output", "all"):
        from .build import build_file

        masters = Masters(Path("master_data")) if Path("master_data").is_dir() else None
        classifications, extractions, drafts = [], [], []
        for pdf in pdfs:
            txts = sorted((work / "ocr").glob(f"{pdf.stem}-*.txt"))
            # De-duplicate page variants (e.g. -1 vs -01 from re-renders).
            seen_pages: dict[str, Path] = {}
            for p in txts:
                key = p.stem.rsplit("-", 1)[-1].lstrip("0") or "0"
                seen_pages.setdefault(key, p)
            ordered = [seen_pages[k] for k in sorted(seen_pages, key=int)]
            texts = [p.read_text(encoding="utf-8", errors="ignore") for p in ordered]
            pids = [str(i + 1) for i in range(len(ordered))]
            dec = file_decision(pdf.name, texts)
            classifications.append(dec)
            if args.stage in ("extract", "build", "output", "all"):
                ext = extract_file(list(zip(pids, texts)))
                if masters is not None:
                    full = "\n".join(texts)
                    vat = (ext["parties"]["vat_ids"] or [""])[0]
                    email = (ext["parties"]["emails"] or [""])[0]
                    ext["master"] = {
                        "supplier_id": masters.match_supplier(
                            ext["parties"]["supplier_name"]["value"], vat, email),
                        "buyer": masters.match_buyer(full),
                    }
                ext["file"] = pdf.name
                extractions.append(ext)
            if args.stage in ("build", "output", "all"):
                drafts.append(build_file(pdf.name, list(zip(pids, texts)), dec, masters))
        (work / "classification.json").write_text(
            json.dumps(classifications, indent=2), encoding="utf-8")
        print(f"Wrote {work / 'classification.json'}")
        if extractions:
            (work / "extraction.json").write_text(
                json.dumps(extractions, indent=2), encoding="utf-8")
            print(f"Wrote {work / 'extraction.json'}")
        if drafts:
            n_ok = 0
            for d in drafts:
                for p in d["payables"]:
                    v = verify_payable(p)
                    n_ok += bool(v["match"])
                    d["diagnostics"].setdefault("erp_check", []).append(
                        {"want": v["want"], "will_book": v["will_book_gross"], "match": bool(v["match"]), "delta": v.get("delta")})
            (work / "drafts.json").write_text(
                json.dumps(drafts, indent=2), encoding="utf-8")
            print(f"Wrote {work / 'drafts.json'}")
            n_pay = sum(len(d["payables"]) for d in drafts)
            print(f"ERP diagnostic: {n_ok}/{n_pay} drafts book to printed gross")

        if args.stage in ("output", "all") and drafts:
            out_dir = Path(args.output_dir)
            if work.name == "output" and args.output_dir == "output" and args.stage == "output":
                out_dir = work
            written = write_output(drafts, out_dir)
            print(f"Wrote {len(written)} files to {out_dir}/")
        from collections import Counter
        print(Counter(c["verdict"] for c in classifications))
        for c in classifications:
            print(f"  {c['file']}: {c['verdict']} ({c['reason']})")


if __name__ == "__main__":
    main()