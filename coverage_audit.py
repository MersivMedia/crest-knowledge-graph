#!/usr/bin/env python3
"""
Measure the real three-way source split across the CREST corpus.

Answers the single largest open question in the project: what fraction of the
934,738 documents is
    (a) on archive.org WITH OCR      -> free, has bounding boxes
    (b) Wayback-only, image PDF      -> you OCR it, cheap
    (c) on neither                   -> the true gap

Prior small samples disagreed badly (55% / 75% / 88% on archive.org), which is
why this exists. Run it with n>=500 before committing to a budget or making a
public completeness claim.

Usage:
    python coverage_audit.py --n 500 --out coverage.json
"""
import argparse, json, os, random, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_document import ia_metadata, ia_file, wayback_pdf, curl

DEFAULT_DB = os.path.expanduser("~/.hermes/data/crest-archive/crest.db")


def audit(db, n, delay, out_path):
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    rows = con.execute(
        "SELECT document_number, pdf_url, pages FROM documents "
        "ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()

    counts = {"ia_ocr": 0, "ia_noocr": 0, "wayback_only": 0, "neither": 0}
    pages = {"ia_ocr": 0, "ia_noocr": 0, "wayback_only": 0, "neither": 0}
    detail = []

    for i, (doc_id, pdf_url, pg) in enumerate(rows, 1):
        meta = ia_metadata(doc_id)
        bucket = None
        if meta:
            has_ocr = bool(ia_file(meta, "Djvu XML") or ia_file(meta, "DjVuTXT"))
            bucket = "ia_ocr" if has_ocr else "ia_noocr"
        else:
            tmp = "/tmp/_cov.pdf"
            bucket = "wayback_only" if (pdf_url and wayback_pdf(pdf_url, tmp)) else "neither"

        counts[bucket] += 1
        pages[bucket] += pg or 0
        detail.append({"doc": doc_id, "bucket": bucket, "pages": pg})

        if i % 25 == 0:
            print(f"  {i}/{n} ...", flush=True)
        time.sleep(delay)

    tp = sum(pages.values()) or 1
    print(f"\nCOVERAGE AUDIT — n={n} sampled from {total:,} documents\n")
    print(f"{'bucket':<16}{'docs':>7}{'doc %':>8}{'pages':>9}{'page %':>9}")
    for k in ("ia_ocr", "ia_noocr", "wayback_only", "neither"):
        print(f"{k:<16}{counts[k]:>7}{counts[k]/n*100:>7.1f}%{pages[k]:>9,}{pages[k]/tp*100:>8.1f}%")

    # Project to the full corpus and cost the OCR gap.
    CORPUS_PAGES = 12_172_653
    need_ocr_frac = (pages["wayback_only"]) / tp
    gap_pages = CORPUS_PAGES * need_ocr_frac
    print(f"\nprojected pages needing OCR : {gap_pages:,.0f}")
    print(f"  self-hosted (25 pg/s, $0.80/h): ${gap_pages/25/3600*0.80:,.0f}"
          f"  ({gap_pages/25/3600:.0f} GPU-hours)")
    print(f"  commercial ($1.50/1k pages)   : ${gap_pages/1000*1.50:,.0f}")
    unreachable = pages["neither"] / tp
    print(f"\nprojected UNREACHABLE pages : {CORPUS_PAGES*unreachable:,.0f} "
          f"({unreachable*100:.1f}%)")

    if out_path:
        json.dump({"n": n, "counts": counts, "pages": pages, "detail": detail},
                  open(out_path, "w"), indent=1)
        print(f"\nwrote {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--delay", type=float, default=0.4, help="be polite to donated infra")
    ap.add_argument("--out", default="coverage.json")
    a = ap.parse_args()
    audit(a.db, a.n, a.delay, a.out)
