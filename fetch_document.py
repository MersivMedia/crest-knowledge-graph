#!/usr/bin/env python3
"""
Fetch a CREST document's text, preferring already-OCR'd sources.

Resolution order (cheapest first):
  1. archive.org _djvu.xml   -> text WITH per-word bounding boxes (free, best)
  2. archive.org _djvu.txt   -> text without boxes (free)
  3. Wayback Machine PDF     -> image-only, needs your own OCR (marked as such)

Returns a normalised record per page so downstream stages never care which
source a document came from.

Usage:
    python fetch_document.py CIA-RDP80T00294A001200090027-4
    python fetch_document.py --db ~/.hermes/data/crest-archive/crest.db --json ID
"""
import argparse, json, os, re, subprocess, sqlite3, sys, urllib.parse, xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
DEFAULT_DB = os.path.expanduser("~/.hermes/data/crest-archive/crest.db")


def curl(url, out=None, timeout=60):
    cmd = ["curl", "-sL", "--compressed", "--max-time", str(timeout), "-A", UA]
    cmd += ["-o", out] if out else ["-o", "-"]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 20)
        return r.stdout.decode("utf-8", "replace") if not out else ""
    except Exception:
        return ""


def ia_metadata(doc_id):
    body = curl(f"https://archive.org/metadata/{doc_id}")
    if len(body) < 5 or body.strip() == "{}":
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def ia_file(meta, fmt):
    """Return a direct, URL-encoded node URL for a derivative format."""
    fn = next((f["name"] for f in meta.get("files", []) if f.get("format") == fmt), None)
    if not fn:
        return None
    # NOTE: filenames routinely contain spaces and commas -> must be encoded,
    # otherwise the request silently fails and you score a 404 page as content.
    return f"https://{meta['server']}{meta['dir']}/{urllib.parse.quote(fn)}"


def parse_djvu_xml(xml_text):
    """Extract per-page words with bounding boxes from an IA _djvu.xml."""
    pages = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return pages
    for pno, obj in enumerate(root.iter("OBJECT")):
        words = []
        for w in obj.iter("WORD"):
            coords = (w.get("coords") or "").split(",")
            if len(coords) >= 4 and w.text:
                words.append({
                    "text": w.text,
                    "bbox": [int(float(c)) for c in coords[:4]],
                })
        if words:
            pages.append({
                "page": pno,
                "width": int(obj.get("width") or 0),
                "height": int(obj.get("height") or 0),
                "words": words,
                "text": " ".join(w["text"] for w in words),
            })
    return pages


def wayback_pdf(pdf_url, out_path):
    """Fetch the raw archived PDF (id_ path bypasses the Akamai challenge)."""
    av = curl(f"https://archive.org/wayback/available?url={urllib.parse.quote(pdf_url, safe='')}")
    m = re.search(r"/web/(\d+)/", av)
    if not m:
        return False
    curl(f"https://web.archive.org/web/{m.group(1)}id_/{pdf_url}", out=out_path, timeout=120)
    try:
        with open(out_path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def fetch(doc_id, pdf_url=None, workdir="/tmp"):
    rec = {"doc_id": doc_id, "source": None, "ocr": None, "pages": [], "needs_ocr": False}

    meta = ia_metadata(doc_id)
    if meta:
        url = ia_file(meta, "Djvu XML")
        if url:
            pages = parse_djvu_xml(curl(url, timeout=120))
            if pages:
                rec.update(source="archive.org", ocr="abbyy", pages=pages)
                return rec
        url = ia_file(meta, "DjVuTXT")
        if url:
            txt = curl(url, timeout=120)
            if txt.strip():
                rec.update(source="archive.org", ocr="abbyy",
                           pages=[{"page": 0, "text": txt, "words": []}])
                return rec

    if pdf_url:
        out = os.path.join(workdir, f"{doc_id}.pdf")
        if wayback_pdf(pdf_url, out):
            # CREST PDFs from Wayback are image-only: 0/15 had a text layer.
            rec.update(source="wayback", ocr=None, needs_ocr=True,
                       pdf_path=out)
            return rec

    return rec


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("doc_id")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    pdf_url = None
    if os.path.exists(a.db):
        con = sqlite3.connect(a.db)
        row = con.execute("SELECT pdf_url FROM documents WHERE document_number=?",
                          (a.doc_id,)).fetchone()
        pdf_url = row[0] if row else None

    r = fetch(a.doc_id, pdf_url)
    if a.json:
        print(json.dumps(r, indent=1)[:4000])
    else:
        print(f"doc      : {r['doc_id']}")
        print(f"source   : {r['source'] or 'NOT FOUND'}")
        print(f"ocr      : {r['ocr'] or ('required' if r['needs_ocr'] else 'n/a')}")
        print(f"pages    : {len(r['pages'])}")
        if r["pages"]:
            p = r["pages"][0]
            print(f"words pg0: {len(p.get('words', []))}")
            print(f"sample   : {p['text'][:160]!r}")
