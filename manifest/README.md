# CREST manifest — mirrored

The complete index of the CIA CREST archive.

```
crest1.zip   41.0 MB   crest_lite_3.csv (174 MB)  +  crest_lite_4.csv (129 MB)
crest2.zip   53.4 MB   crest_lite_1.csv (204 MB)  +  crest_lite_2.csv (184 MB)
             ─────────
             94.3 MB   4 CSVs, 691 MB uncompressed
```

**934,738 documents · 12,172,653 pages.** Every field 100% populated.

## Why this is committed

Normally you would not put 94 MB of data in a git repo. This is the exception.

The only public copy lives in [`morisy/ci-trend-explorer`](https://github.com/morisy/ci-trend-explorer)
— an unmaintained repository with 3 stars, last pushed in 2017, belonging to a
single personal account. Michael Morisy founded MuckRock, whose three-year
lawsuit forced the CIA to put CREST online.

It took a lawsuit and nine years for this index to exist publicly. It should
not depend on one account staying up. **Mirror it again yourself.**

## Verify

```bash
sha256sum -c SHA256SUMS
```

```
8aba3080c8f8ccf665f606948b9a34fc692562de0c4bda467037d72e8311c638  crest1.zip
cf01e982972f5cf0c3692c3c41e854244070352fc3dc7ea0deafcde167ac232c  crest2.zip
```

## Fields

Every row carries all of these, with no nulls:

```
document_number          CIA-RDP80T00294A001200090027-4
title                    HUMPHREY-SCOTT CODEL MEETING WITH BREZHNEV
file                     PDF URL (2017 /library/readingroom/ form — rewrite to /readingroom/)
url                      landing page
document_page_count      integer
document_creation_date   declassification date
document_publication_date
publication_date         original document date
content_type             REPORT · MEMO · CABLE · LETTER · NOTES · MFR · MF · NSPR · MAP
collection               e.g. "General CIA Records"
document_type            CREST
release_decision         e.g. RIPPUB
case_number · sequence_number
```

`content_type` is pre-existing structure worth exploiting — it becomes a node
property with no extraction cost.

## Page distribution

Severely skewed. Size every batch and timeout for the tail:

```
mean 13.02 · median 2 · p90 42 · p99 100 · max 3,167
5,304 documents (0.57%) exceed 100 pages and hold 9.4% of all pages
```

CSVs 1 and 4 hold the bulk volumes (mean 21 and 23 pages/doc); CSVs 2 and 3
average under 6.

## Use

```bash
cd ..
python manifest_to_sqlite.py --data-dir manifest
```

Produces an indexed SQLite database (~498 MB) at
`~/.hermes/data/crest-archive/crest.db`.

## Provenance and licence

Source: `morisy/ci-trend-explorer`, MIT licensed. The underlying CIA documents
are US government works in the public domain. This mirror is verbatim — no
rows added, removed or altered.
