---
name: cia-crest-archive
description: "Ingest the CIA CREST declassified archive (934k docs)."
version: 1.0.0
author: Nous Research
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [CREST, CIA, declassified, OCR, archive, knowledge-graph, FOIA]
    related_skills: [grounded-citations, ocr-and-documents]
---

# CIA CREST Archive

## When to Use

Load this when the task involves the CIA's declassified CREST archive or the
cia.gov FOIA reading room — ingesting it, searching it, building a knowledge
graph or dataset over it, or costing such a project. Also load it when asked
to OCR or entity-extract a large scanned government corpus, since §6's cost
structure (OCR is cheap, extraction is not) generalises.

Do **not** reach for cia.gov directly; it is bot-walled (§3, §4).

Acquiring, OCR-sourcing and costing the CIA's declassified CREST corpus —
934,738 documents / 12,172,653 pages.

The central insight: **nearly all of this is already OCR'd by someone else.**
Do not plan a 12-million-page OCR project until you have measured how much of
it you actually need to do. Measured answer so far: somewhere between 12% and
77% — see §5, the number is genuinely unsettled.

## Scripts

- `scripts/manifest_to_sqlite.py` — build an indexed SQLite DB of all 934,738 docs
- `scripts/fetch_document.py` — fetch one doc's text, preferring OCR'd sources
- `scripts/coverage_audit.py` — measure the three-way source split (run this first)

---

## 1. The manifest — get this first

The complete CREST index lives in an unmaintained GitHub repo with 3 stars:

```bash
curl -sL -o crest1.zip "https://raw.githubusercontent.com/morisy/ci-trend-explorer/master/Crest%201.zip"
curl -sL -o crest2.zip "https://raw.githubusercontent.com/morisy/ci-trend-explorer/master/Crest%202.zip"
```

94 MB, four CSVs, **934,738 rows / 12,172,653 pages**. Michael Morisy founded
MuckRock, whose lawsuit forced the CREST release — this is the authoritative
index, not a sample.

**Mirror it immediately.** It took a lawsuit and nine years to exist and it
hangs off one personal account. Store under `~/.hermes/data/crest-archive/`
with checksums.

Every field is 100% populated:

```
document_number · title · file (PDF URL) · document_page_count
document_creation_date · publication_date · content_type · collection
document_type · release_decision · case_number · sequence_number
```

```bash
python scripts/manifest_to_sqlite.py     # -> crest.db, ~498 MB, indexed
```

### Page distribution is brutally skewed

```
mean 13.02  ·  median 2  ·  p90 42  ·  p99 100  ·  max 3,167
5,304 docs (0.57%) are >100 pages and hold 9.4% of all pages
```

Size every batch, timeout and cost estimate for a 3,000-page document.
CSV files 1 and 4 hold the bulk volumes (mean 21 and 23 pages); files 2 and 3
average under 6.

---

## 2. Source #1 — archive.org (free OCR **with bounding boxes**)

```
275,008 CREST items under identifier:CIA-RDP*
```

Every sampled item ships ABBYY derivatives:

| File | Contents |
|---|---|
| `_djvu.xml` | **per-word bounding boxes**, page-segmented, 300 DPI |
| `_djvu.txt` | plain OCR text |
| `_abbyy.gz` | full ABBYY output |
| `.pdf` / `_jp2.zip` | original scan / page images |

```xml
<OBJECT height="3301" width="2550">
  <PARAM name="DPI" value="300"/>
  <WORD coords="540,383,744,342,374">Approved</WORD>
```

**This solves page-level citation for free.** If your product must link a
search hit to the exact page (and highlight the word), take the `_djvu.xml`
and never flatten it — retrofitting coordinates means re-running everything.

Measured OCR quality: **93.2% word-plausibility** (n=6, range 90–97%).

### Pitfall: filenames contain spaces and commas

`MARBACH, KARL HEINZ_0001_djvu.txt` — unencoded requests return an HTML 404
page that downstream code happily parses as text. I scored three such error
pages as "95.8% quality OCR" before catching it. Always `urllib.parse.quote`
the filename from `/metadata/<id>`, and verify content, not just status.

### Bulk enumeration

Use the **scrape API**, not paged search. Paged search silently returns
partial results — it gave page-count means of 8.63, then 2.37, then 4.00 on
the same query. The scrape API over 24,154 items gave 4.00 ± 0.18.

```
https://archive.org/services/search/v1/scrape?q=identifier%3ACIA-RDP*&fields=imagecount&count=10000
```

---

## 3. Source #2 — Wayback Machine (bypasses the bot wall, **no OCR**)

cia.gov is behind Akamai Bot Manager; every scripted request returns a
`bm-verify` interstitial. Wayback's raw-content path serves the archived PDF
directly:

```
https://web.archive.org/web/<timestamp>id_/<cia.gov pdf url>
```

Resolve a timestamp via `https://archive.org/wayback/available?url=<encoded>`.

### CREST PDFs from Wayback have NO text layer

```
15 valid CREST PDFs tested  ->  0/15 had embedded text  (100% image-only)
                                5/20 had no snapshot at all
```

**Do not confuse two different corpora on the same host.** Captures under
`/readingroom/docs/` with *non*-CIA-RDP filenames (AERODYNAMIC, OSS/Nazi
war-crimes files, date-named daily briefs) DO carry embedded OCR — 92% in a
24-file sample. Those are separately processed releases. CREST proper is 1990s
microfilm digitisation that the Agency never OCR'd. Measuring one and
generalising to the other is an easy and costly mistake.

### URL form changed

The 2017 manifest uses `/library/readingroom/`; the current form drops
`/library`. Rewrite before resolving:

```python
url.replace("/library/readingroom/", "/readingroom/")
```

---

## 4. Sources that do NOT work

| Source | Why not |
|---|---|
| **cia.gov direct** | Akamai `bm-verify` on every scripted request, including `/advanced-search-view`. Works in a browser only. |
| **Magda** | A *data catalog* (dataset discovery/governance). No OCR, no entity extraction, no semantic search. Not comparable to a vector DB. |
| **Perplexity API** | No ingestion endpoint; searches the live web; best on text-native PDFs (ours are images); `citations` often empty even with `return_citations=true`. Cannot satisfy page-level citation. |
| **DeclassDB** | Every `/doc/` permalink from its own RSS returns 404. "mkultra" returned 1 document and 15 scraped search-listing pages with synthetic IDs (`CIA-URL-READINGROOM-KEYWORD-HYPNOSIS`). Zero results for STARGATE. Claims "1.05M documents" and "126,000+ records" in one stat block. |
| **rva5120/cia-crest-explorer** | ~1,000 OCR'd JPRS docs. Reference implementation, not a source. |

---

## 5. Coverage is UNRESOLVED — measure before budgeting

Four samples, four answers for "on archive.org":

| n | result | how sampled |
|---|---|---|
| 60 | 55% | one CDX page, skewed to RDP55/56 |
| 25 | 88% | random from manifest |
| 20 | 75% | random from manifest |
| 30 | 23% | random from manifest (page-weighted: 9.8%) |

That spread is not noise to average away — it means small samples are
unreliable here, probably because coverage varies sharply by RDP series
(archive.org holds 131,825 RDP8* items but only 9,727 RDP5*).

```bash
python scripts/coverage_audit.py --n 500 --delay 0.4
```

~20 minutes of paced requests. Returns the three-way split
(archive.org+OCR / Wayback-only / neither) by both document and **page**
count, and projects the OCR bill. Do this before committing a budget or making
any public completeness claim.

---

## 6. Cost model (12,172,653 pages)

### Rent the GPU, not the tokens

The naive plan — OCR, then a per-page API call for entity extraction — costs
**$9,495**, because 12.2M calls × $0.00078 adds up even at the cheapest tier.
The cost is the *call count*, not the price per call. No model shopping fixes
it.

Self-hosting an open-weight VLM removes per-call pricing entirely, and lets
one pass do transcription + entities + visual elements together:

| Setup | pages/sec | days | Total |
|---|---|---|---|
| 1× H100 · 8B | 3.5 | 40.3 | $2,415 |
| 4× H100 · 8B | 13.0 | 10.8 | $2,601 |
| **8× H100 · 8B** | **25.0** | **5.6** | **$2,705** |
| 8× H100 · 30B MoE | 10.0 | 14.1 | $6,763 |

**Cost is nearly flat in GPU count — you buy wall-clock, not compute.**
(Throughput estimated from typical batched vLLM decode; benchmark before
committing.)

### Full-archive total

```
acquisition (6-11 days bandwidth)          $0
one-pass VLM over all 12.2M pages      $2,705
embeddings                               $292
storage, 6 months                        $504
                                       -------
CORE TOTAL                             $3,501
optional LLM relation pass on 15%      $1,424
```

### Model choice

**Qwen3-VL** — OCR in 32 languages, explicitly robust to low light, blur and
tilt (exactly what 1950s microfilm produces), improved long-document structure
parsing. OCRBench 896 vs Gemma-3's 480. Alternatives: InternVL3, Pixtral, Molmo.

**Not Grok.** xAI's open weights (Grok-1, Grok-2) are **text-only** — no
released multimodal weights, so it cannot read page images.

### Run vision on 100%, not just the gap

```
gap only (~45% of pages)     61 h   $1,217
every page                  135 h   $2,705
```

~$1,500 buys complete visual coverage and eliminates per-page "does this
deserve vision?" heuristics. More importantly it captures what OCR structurally
misses: maps, photographs, diagrams, seals, redaction bars, routing stamps and
handwritten marginalia.

### Drop word-level bounding boxes

A VLM returns text and entities, not coordinates. If citations resolve to
**document + page** (enough for any reader — link the PDF at that page), you
gain:

- independence from archive.org's `_djvu.xml`, hence from the coverage question
- uniform vision over every page
- one code path instead of two

Only keep ABBYY boxes if in-page word highlighting is a hard product
requirement.

### Other

```
corpus size    ~3.65 TB (3.9 MB/doc measured)
egress         $0 — but be polite: 1-2 req/s => 6-11 days
storage        $84/mo object storage, or ~$250 one-time 8TB drive
recurring      $125-215/mo (VPS + storage + CDN)
```

Acquisition (6-11 days) and inference (5.6 days) overlap — start transcribing
as documents land.

### Always pilot first

One program (STARGATE, ~90k pages) exercises the whole pipeline **with vision**
for under $25 — 1 hour on 8×H100, or 7 hours on one. The architecture is
identical at 90k and 12.2M pages; the corpus is a swappable input.

Benchmark before scaling: one H100 for one hour (~$2.50) gives you real
pages/sec, a head-to-head against ABBYY on the *same* degraded pages, and a
hallucination rate on illegible scans. Never commit 5.6 GPU-days on an
estimate.

### Watch for VLM hallucination

The failure modes differ, and the VLM's is more dangerous:

```
OCR    fails visibly — garbled characters you can detect
VLM    can invent plausible text on an illegible page
```

Require a per-page `legibility` score in the structured output, sample-audit
the low scores against the page image, and always link the scan so a reader can
check the machine's work.

---

## 7. Lessons that cost real time

- **Verify content, not status codes.** archive.org returns HTML error pages
  that parse as text. A 404 page scored as "95.8% quality OCR" until checked.
- **Small samples lie here.** Page-count means of 8.63 / 2.37 / 4.00 from the
  same query; coverage of 23% / 55% / 75% / 88%. Use the scrape API and n≥500.
- **`subject:CREST` on archive.org is contaminated** — it returns TV news
  broadcasts (`MSNBCW_20180216_..._The_Rachel_Maddow_Show`, 3,661 "pages").
  Filter on `identifier:CIA-RDP*` instead. Using the subject tag produced a
  nonsense estimate of 498 million pages.
- **Don't generalise OCR presence across corpora** on the same host (§3).
- **Preserve bounding boxes end to end** or lose citation linking permanently.
- **Regex-filtered CDX queries** (`filter=original:.*CIA-RDP.*`) will 503
  Wayback. Page slowly.
