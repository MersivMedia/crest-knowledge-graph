#!/usr/bin/env python3
"""
Load the CREST manifest (934,738 documents) into a queryable SQLite database.

The manifest ships as four CSVs inside two zips recovered from
github.com/morisy/ci-trend-explorer. This builds an indexed SQLite copy so the
rest of the pipeline can query it without re-parsing 300MB of CSV.

Usage:
    python manifest_to_sqlite.py [--data-dir DIR] [--db PATH]
"""
import argparse, csv, io, os, sqlite3, sys, zipfile

DEFAULT_DATA = os.path.expanduser("~/.hermes/data/crest-archive")

FIELDS = [
    "document_number", "title", "file", "url", "document_type", "collection",
    "release_decision", "document_page_count", "document_creation_date",
    "document_publication_date", "sequence_number", "case_number",
    "publication_date", "content_type",
]


def modernise(url: str) -> str:
    """The 2017 manifest uses /library/readingroom/; current form drops /library."""
    return (url or "").strip().replace("/library/readingroom/", "/readingroom/")


def build(data_dir: str, db_path: str) -> None:
    zips = [os.path.join(data_dir, z) for z in ("crest1.zip", "crest2.zip")]
    missing = [z for z in zips if not os.path.exists(z)]
    if missing:
        sys.exit(f"missing manifest zips: {missing}\nSee SKILL.md §Acquiring the manifest")

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"""CREATE TABLE documents (
        {', '.join(f'{f} TEXT' for f in FIELDS)},
        pdf_url TEXT,
        pages INTEGER
    )""")

    total = 0
    for zf in zips:
        z = zipfile.ZipFile(zf)
        for name in z.namelist():
            if not name.endswith(".csv") or name.startswith("__MACOSX"):
                continue
            with z.open(name) as fh:
                rd = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
                batch = []
                for row in rd:
                    try:
                        pages = int(row.get("document_page_count") or 0)
                    except ValueError:
                        pages = 0
                    batch.append(
                        tuple(row.get(f, "") for f in FIELDS)
                        + (modernise(row.get("file", "")), pages)
                    )
                    if len(batch) >= 5000:
                        con.executemany(
                            f"INSERT INTO documents VALUES ({','.join('?' * (len(FIELDS) + 2))})",
                            batch)
                        total += len(batch); batch = []
                if batch:
                    con.executemany(
                        f"INSERT INTO documents VALUES ({','.join('?' * (len(FIELDS) + 2))})",
                        batch)
                    total += len(batch)
            print(f"  loaded {name}  (running total {total:,})")

    print("  building indexes ...")
    con.execute("CREATE UNIQUE INDEX idx_docnum ON documents(document_number)")
    con.execute("CREATE INDEX idx_pages ON documents(pages)")
    con.execute("CREATE INDEX idx_ctype ON documents(content_type)")
    con.execute("CREATE INDEX idx_collection ON documents(collection)")
    con.commit()

    docs, pages, mx = con.execute(
        "SELECT COUNT(*), SUM(pages), MAX(pages) FROM documents").fetchone()
    print(f"\n  documents : {docs:,}")
    print(f"  pages     : {pages:,}")
    print(f"  largest   : {mx:,} pages")
    print(f"  db        : {db_path} ({os.path.getsize(db_path)/1e6:.0f} MB)")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--db", default=os.path.join(DEFAULT_DATA, "crest.db"))
    a = ap.parse_args()
    build(a.data_dir, a.db)
