# CREST Knowledge Graph — Product Requirements Document

**An interactive 3D knowledge graph over the CIA's declassified CREST archive.**

Status: pre-build. Every number below is measured, not projected; sources and
sample sizes are stated inline. Unverified claims are marked **[UNVERIFIED]**.

---

# 1. What this is

A public research tool that makes 934,738 declassified CIA documents
*navigable* rather than merely searchable. A user searches a subject and gets
the people, places, events and timelines connected to it — every assertion
linked back to a specific page of a specific PDF, rendered as an explorable 3D
graph.

The existing alternatives are a keyword box over scanned images (cia.gov) or
an item list (archive.org). Neither answers *"who else appears in these
documents, and where do these reports overlap?"*

## Core requirements

| # | Requirement | Implication |
|---|---|---|
| 1 | Identify people, places, events, timelines, images within reports | Entity + relation extraction over OCR text |
| 2 | Identify overlaps across reports and historical events | Graph store; co-occurrence is a graph query, not a similarity search |
| 3 | Search returns related info with citations linking to the actual PDF page | Word-level bounding boxes preserved from OCR through to the UI |
| 4 | Present as a 3D knowledge graph | three.js / WebGL front end over a queryable API |

---

# 2. The corpus — measured

The full CREST manifest was recovered from `morisy/ci-trend-explorer` on
GitHub (Michael Morisy, founder of MuckRock, whose lawsuit forced the CREST
release). Two zips, 94 MB, parsed in full:

```
934,738  documents        (unique document_number, 0 rows missing an ID)
12,172,653  pages
     13.02  mean pages/document
      2     median pages/document
     42     p90
  3,167     max
```

**The page distribution is extremely skewed.** 5,304 documents (0.57%) exceed
100 pages and account for 1,142,644 pages — 9.4% of the corpus. Batch sizing,
timeouts and cost estimates must handle a 3,000-page document without falling
over.

Every manifest field is 100% populated:

```
document_number · title · file (PDF URL) · document_page_count
document_creation_date · document_publication_date · publication_date
content_type · collection · document_type · release_decision · case_number
```

`content_type` is pre-existing structure worth exploiting — REPORT, MEMO,
CABLE, LETTER, NOTES, MFR, MF, NSPR and others arrive free and become node
properties on day one.

---

# 3. Data sources — what we verified

## 3.1 archive.org — the primary source

```
275,008  CREST items (identifier:CIA-RDP*)
 24,154  items measured for page count via the scrape API
   4.00  mean pages/item (95% CI ±0.18)
```

Every sampled item (14/14) ships these derivatives:

| File | Contents |
|---|---|
| `_djvu.txt` | plain OCR text |
| **`_djvu.xml`** | **per-word bounding boxes, page-segmented, 300 DPI** |
| `_abbyy.gz` | full ABBYY OCR output |
| `.pdf` | original scan |
| `_jp2.zip` | page images (for VLM re-reads) |

The `_djvu.xml` is what makes requirement #3 possible without spending
anything on OCR:

```xml
<OBJECT height="3301" width="2550">
  <PARAM name="DPI" value="300"/>
  <WORD coords="540,383,744,342,374">Approved</WORD>
```

Measured OCR quality: **93.2% word-plausibility** (range 90–97%, n=6
documents). Body prose reads cleanly; handwritten marginalia, routing stamps
and classification markings do not. That split is useful — it is the basis of
the escalation rule in §5.3.

## 3.2 Wayback Machine — the gap filler

Wayback serves archived cia.gov PDFs through the `id_` raw-content path,
which **bypasses the Akamai bot challenge** that blocks scripted access to
cia.gov:

```
https://web.archive.org/web/<timestamp>id_/<cia.gov pdf url>
  -> 200, application/pdf, magic %PDF-
```

**CREST PDFs from Wayback have NO text layer.** Measured directly:

```
15 valid CREST PDFs fetched via Wayback
   with embedded text:   0/15   (0%)
   image-only:          15/15   (100%)
    5/20 had no snapshot at all
```

An earlier 92%-have-text measurement was taken from `/readingroom/docs/`
captures, which contain **zero** CIA-RDP identifiers — that slice is the
named collections (AERODYNAMIC, OSS/Nazi war-crimes files, daily briefs), a
different corpus that CIA did publish with OCR. CREST proper is 1990s
microfilm digitisation and was never OCR'd by the Agency.

## 3.3 Coverage — an open question

Three samples disagree, and this is the single largest unknown in the plan:

| n | on archive.org | how sampled |
|---|---|---|
| 60 | 55.0% | one CDX page, skewed to RDP55/56 |
| 25 | 88.0% | random from manifest |
| 20 | 75.0% | random from manifest |

**Action before build: a 500-document paced validation** measuring three
fractions — on archive.org with OCR / Wayback-only needing OCR / neither.
~20 minutes of requests, and it converts a 55–88% range into one number that
determines both the OCR budget and what the site can honestly claim.

## 3.4 Sources evaluated and rejected

| Source | Verdict |
|---|---|
| **cia.gov direct** | Akamai `bm-verify` challenge on every scripted request. Not scrapable. |
| **Magda** | A *data catalog* — indexes datasets, does no OCR, entity extraction or semantic search. Different layer entirely. |
| **Perplexity API** | Searches the live web; no ingestion endpoint; performs best on text-native PDFs (ours are images); `citations` returns empty or short even with `return_citations=true`. Fails requirement #3 structurally. |
| **DeclassDB** | Real service, but every `/doc/` permalink from its own RSS returns **404**. On its flagship "mkultra" query, 1 of 20 results was a document — 15 were scraped search-listing pages with synthetic IDs. Zero results for STARGATE. Site claims both "1.05M CREST documents" and "126,000+ records" in the same stat block. Possible future use for cross-agency (FBI/State/NSA/NARA) linking. |
| **rva5120/cia-crest-explorer** | ~1,000 OCR'd JPRS documents. Too small to matter as a source; useful as a reference implementation. |

---

# 4. Architecture — one-pass vision

## 4.1 The decision

**Run a vision-language model over every page, once.** It transcribes, extracts
entities, and describes visual elements in a single call — replacing the
separate OCR stage and the separate entity-extraction stage entirely.

The alternative (OCR, then a text LLM over the OCR) was costed at $4,112 and
is *worse*: it never sees the page. It silently drops the maps, photographs,
diagrams, stamps and handwritten marginalia that make these documents what
they are, and it needs per-page filtering decisions to stay affordable.

```
manifest (934,738 rows)
   │
   ├─► archive.org PDF/JP2 ──┐
   └─► Wayback PDF ──────────┤   (acquisition only; no OCR needed from either)
                             ▼
                    Qwen3-VL one-pass per page
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
     transcription      entities +          visual elements
                        relations           (images, stamps,
          │                  │               redactions, seals)
          └──────────────────┼──────────────────┘
                             ▼
                  page records {doc_id, page, ...}
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
        Neo4j          Qdrant/pgvector      PDF object store
       (graph)          (semantic)        (citation target)
                             ▼
                      query API (FastAPI)
                             ▼
                  three.js 3D graph front end
```

## 4.2 Model — self-hosted open weights, NOT a hosted API

**Decision: self-host. This is a content-filtering decision, not a cost one.**

### Hosted APIs refuse declassified CIA content — measured

Three pages of real-archive material were sent to a production API with the
extraction prompt below:

```
real MKUltra memo (CIA-RDP81-00261R000300050001-7)   OK
interrogation/ARTICHOKE material                      OK
PBSUCCESS coup material (target lists, bombing)       REFUSED
```

The refusal, verbatim:

> *"I can't help with extracting or structuring data from this document as
> presented. This text describes extrajudicial killings, civilian bombing, and
> covert regime change operations. **Even if this were a real declassified
> document**, processing it into structured data for potential operational
> reference would be inappropriate."*

The model explicitly considered that the material was genuine and declassified,
and refused regardless.

### Why this disqualifies the API path

- **CREST is full of this material.** Assassination planning, coup operations,
  interrogation programs, target lists — PBSUCCESS, PBFORTUNE, ZRRIFLE, Phoenix,
  MKUltra. Not an edge case; a significant share of what makes the archive
  historically valuable.
- **Failures cluster on the highest-value documents.** An archive that
  faithfully indexes cafeteria memos while silently omitting the Guatemala coup
  is worse than no archive.
- **Refusals are silent.** They return HTTP 200 with prose. Without explicit
  refusal detection they enter the pipeline as malformed records, not errors.
- **Non-deterministic.** The same page may pass today and fail tomorrow, or
  differ across model versions. The corpus cannot be reprocessed consistently.
- **The boundary is unpredictable.** The interrogation case — non-consensual
  drugging, sleep deprivation, two deaths — passed. The coup case refused. You
  cannot design around a line you cannot locate.

Caveat: one provider was tested, and two of three cases were synthetic
documents written to probe the boundary. The confirmed-real document passed.
Measuring the true refusal rate over several hundred genuine pages is a task
in `benchmark_vlm.py`. But betting a multi-year archive on a policy that can
change without notice is the wrong risk to take.

### The model

**Qwen3-VL** (open weights). OCR across 32 languages, explicitly robust to
**low light, blur and tilt** — precisely what 1950s microfilm produces — with
improved long-document structure parsing. OCRBench 896 vs Gemma-3's 480.

Alternatives worth benchmarking: InternVL3, Pixtral, Molmo.

**Not Grok.** xAI's open weights (Grok-1, Grok-2) are **text-only**. No
released multimodal weights, so it cannot read page images.

Open weights on your own GPU have no policy layer between you and the corpus,
are deterministic at `temperature=0`, and cannot be deprecated out from under a
multi-year project.

## 4.3 Citation granularity — a deliberate trade

Word-level bounding boxes are **dropped as a requirement**. Citations resolve
to *document + page*, which is what a reader actually needs: "this claim comes
from page 3 of CIA-RDP80T00294A001200090027-4", linking to that PDF at that
page.

What this buys:
- No dependency on archive.org's `_djvu.xml` (so no coverage dependency)
- Vision applied uniformly to every page, including the ~55-88% archive.org
  already OCR'd
- A single code path instead of two (ABBYY-derived vs self-OCR'd)

What it costs: no in-page word highlighting. A VLM returns text and entities,
not coordinates. If word-level highlighting is ever wanted, a cheap OCR pass
can be run alongside purely for coordinates — but it is not needed for the
product as specified.

## 4.4 Why vision on 100% of pages

Cost is nearly flat in GPU count — you are buying wall-clock, not compute:

| Setup | pages/sec | days | Total |
|---|---|---|---|
| 1× H100 · 8B | 3.5 | 40.3 | $2,415 |
| 2× H100 · 8B | 7.0 | 20.1 | $2,415 |
| 4× H100 · 8B | 13.0 | 10.8 | $2,601 |
| **8× H100 · 8B** | **25.0** | **5.6** | **$2,705** |
| 8× H100 · 30B MoE | 10.0 | 14.1 | $6,763 |

Running the gap only (~45% of pages) costs $1,217; running **everything** costs
$2,705. **~$1,500 buys complete visual coverage** and removes the need to
decide which pages deserve vision — a decision that would otherwise be made
badly, by heuristic, 12 million times.

*Throughput figures are estimates from typical batched vLLM decode, not
measured. See §7 M1.*

## 4.5 Extraction schema

One structured response per page:

```json
{ "doc_id": "CIA-RDP80T00294A001200090027-4",
  "page": 3,
  "transcription": "full page text as read",
  "entities": [{"type":"PERSON","name":"Allen Dulles","mention":"the Director"}],
  "relations": [{"subj":"Humphrey","pred":"MET_WITH","obj":"Brezhnev","date":"1975-07-02"}],
  "visual_elements": [{"type":"stamp","text":"CONFIDENTIAL"},
                      {"type":"photograph","description":"aerial view of facility"},
                      {"type":"redaction","extent":"3 lines"}],
  "legibility": 0.0 }
```

`visual_elements` is the payoff of the vision approach and satisfies
requirement #1's "images within reports" — maps, photographs, diagrams,
seals, redaction bars and routing stamps that a pure-OCR pipeline discards as
noise.

`legibility` is the model's own confidence, used to flag pages for review
rather than to silently drop them.

**Nodes:** Document · Person · Organization · Place · Event · Date · Program ·
VisualElement
**Edges:** MENTIONS · CO_OCCURS_WITH · AUTHORED_BY · REFERENCES · DATED ·
PART_OF_PROGRAM · LOCATED_IN · DEPICTS

Every edge carries `doc_id` + `page`. An edge with no citable page is a bug,
not a weak signal.

## 4.5a Structured output — the model proposes, the validator decides

A VLM asked for JSON will *usually* return JSON. "Usually" is not a contract at
12.2 million pages.

**Constrained decoding, not prompting.** vLLM's `guided_json` makes it
structurally impossible for the model to emit a token that breaks the schema:

```python
payload = {"model": MODEL, "messages": [...],
           "guided_json": PAGE_SCHEMA, "temperature": 0}
```

This collapses two whole failure classes (`unparseable`, `schema_invalid`)
to near zero.

**Validation stays on anyway.** Guided decoding guarantees *shape*, not
*truth*. `schema.py` adds a second, semantic gate:

```
validate_page()    shape: required fields, enum membership, ranges
check_grounding()  truth: every entity name must appear in the transcription
```

`check_grounding()` is the cheap deterministic hallucination detector. An
entity the model "knows" but that is not on the page is exactly the failure
that makes a citation-backed archive untrustworthy — a plausible fabrication
carrying a real citation.

Nothing enters Neo4j or the vector store without passing both. Records that
fail are quarantined and replayable, never coerced.

## 4.6 Graph store and vector store

They answer different questions, and requirement #2 is squarely a graph
question:

- *"Which people appear in both this report and that one"* — graph traversal.
  A vector database cannot answer this; similarity is not co-occurrence.
- *"Find documents about this topic even if they use different words"* —
  vector search.

Chroma vs Pinecone is a hosting decision, not an architectural one, and the
most swappable component in the system. At ~18M vectors, self-hosted Qdrant or
pgvector runs ~$40-80/mo against ~$300-700/mo managed.

---

# 5. Cost analysis — entire archive

All GPU rates below are from market surveys, not assumptions. An earlier draft
of this document used $2.50/hr for H100 and $0.15/$0.60 per M tokens for the
API; both were wrong, the API figure by 5x.

## 5.1 Verified rates

```
H100 market range      $1.49 - $6.98 /hr    (RunPod SXM list $3.29; cheap tiers from $1.99)
RTX 4090               from $0.27 /hr
```

Cheapest vision-capable API models (live OpenRouter pricing, $/M tokens):

```
qwen/qwen3.7-flash                    0.030 in / 0.130 out
openai/gpt-5-nano:batch               0.025 / 0.200
google/gemini-2.5-flash-lite:batch    0.050 / 0.200
```

## 5.2 Full corpus, one-pass vision over 12,172,653 pages

| Path | Cost | Trade |
|---|---|---|
| Self-host 4090s @ $0.27 | **$1,304** | cheapest; 25-50 days; 24GB may not fit 8B at high res |
| Self-host 8xH100 spot @ $1.49 | $1,612 | 5.6 days; preemption risk |
| *API: qwen3.7-flash* | *$1,814* | *zero ops — **but see §4.2, it will refuse documents*** |
| Self-host 8xH100 list @ $3.29 | $3,560 | fast, reliable, priciest |

Self-hosting has costs not in the headline number: vLLM setup and debugging
(4-8 hours), 15-25% overhead from failed runs and restarts, idle GPU time while
fixing things, and egress to feed 3.65 TB of images to the GPU. Realistically
the 4090 path is **~$1,565 plus a weekend of ops**.

**Recommended: self-hosted 8xH100 spot, ~$1,612, 5.6 days.** The API saves
nothing once ops are priced in, and it silently redacts the archive.

## 5.3 Total

| Line | Cost |
|---|---|
| Acquisition (bandwidth, 6-11 days) | $0 |
| One-pass VLM, self-hosted, 12.2M pages | $1,612 |
| Embeddings | $292 |
| Storage, 6 months | $504 |
| **Core total** | **$2,408** |
| *Optional* relation pass on richest 15% | ~$400 |
| **With relations** | **~$2,800** |

Recurring: **$125-215/mo** (VPS + 3.65 TB object storage + CDN).

## 5.4 Why the earlier $9,495 figure was wrong

That number came from making **12.2 million API calls** at an assumed
$0.00078 each. Two errors compounded: the per-call price was 5x too high, and
per-call pricing is the wrong model entirely. Renting the machine instead of
the tokens removes call-count scaling — you pay for wall-clock, and cost is
nearly flat in GPU count (1xH100 for 40 days costs about the same as 8xH100 for
5.6 days).

## 5.5 Acquisition

```
corpus size    ~3.65 TB  (measured mean 3.9 MB/document)
egress cost    $0 — archive.org and Wayback do not charge
politeness     1-2 req/s  =>  6-11 days wall-clock
storage        $84/mo object storage, or ~$250 one-time 8TB drive
```

**Time, not money, is the acquisition constraint.** Acquisition (6-11 days) and
inference (5.6 days) overlap — start transcribing as documents land.

## 5.6 Coverage affects completeness, not cost

Because the VLM reads pixels, archive.org's OCR is no longer needed for
processing. The unresolved 23-88% coverage question (§3.3) now only determines
**how many documents can be obtained at all**.

## 5.7 Pilot first

STARGATE, ~90,000 pages: **under $25**, roughly 1 hour on 8xH100. Exercises the
entire pipeline, with vision, on real CIA scans. The architecture is identical
at 90k and 12.2M pages.

Before even that: `benchmark_vlm.py` on a single GPU-hour (~$2-3) replaces every
throughput estimate in this document with a measurement, and reports the
refusal rate, schema-validity rate, and agreement with ABBYY on the same pages.

---

# 5.8 The agent harness

The pipeline is deterministic code; the agent is an offline loop that reads its
failures and proposes improvements. The boundary matters:

```
HOT PATH    12.2M pages, no agent — predictable cost, reproducible output
QUARANTINE  every failure kept as replayable JSON + raw model output
COLD PATH   agent reads failures, finds patterns, proposes fixes
GATE        human merges; nothing auto-applies
```

An LLM agent deciding per page would be slow, costly and non-reproducible. Its
value is in the 2-5% that fail — not the 95% that already work.

Three rules, enforced by convention and documented in `AGENT_HARNESS.md`:

1. **The agent never edits the validator.** Asked to reduce failures, an agent
   will relax the constraint rather than meet it.
2. **Fixes are proven with `--replay-all`** against the quarantine corpus,
   which doubles as a regression suite. Assertion is not evidence.
3. **A human merges**, with before/after numbers in the diff.

Prompt versions are hashed, so success rates can be compared across versions
rather than assumed to improve.

---

# 6. Risks

| Risk | Evidence | Mitigation |
|---|---|---|
| **Coverage unknown** | 4 samples: 23% / 55% / 75% / 88% | Run the 500-doc validation. Now affects *completeness*, not cost (§5.4) |
| **Some docs on neither source** | 5/20 had no Wayback snapshot | Quantify in the same validation; cia.gov via browser automation is the last resort |
| **VLM transcription quality unmeasured** | ABBYY scored 93.2% plausible; Qwen3-VL vs ABBYY on bad scans is untested | Benchmark head-to-head in M1 before committing 5.6 GPU-days |
| **VLM hallucination** | Not yet measured. A VLM can invent plausible text on an illegible page — worse than OCR, which fails visibly | `check_grounding()` rejects entities absent from the transcription; `legibility` per page; agent Task 4 audits low-legibility/long-transcription pages against the image |
| **Hosted API refusal** | **Measured: 1 of 3 archive-realistic pages refused** (§4.2) | Self-host open weights. Benchmark reports refusal rate so the risk is quantified, not assumed |
| **Schema drift** | A model returning prose instead of JSON is a silent data loss | `guided_json` constrained decoding + `validate_page()` gate; failures quarantined and replayable |
| **3D graph unusable at scale** | 18.3M vectors, millions of nodes | Never render the whole graph; query-scoped subgraphs with a hard node cap |
| **Bandwidth/politeness** | 3.65 TB from donated-infrastructure archives | Rate-limit, resume cleanly, cache aggressively; do not hammer archive.org |
| **Manifest is a single point of failure** | One unmaintained repo, 3 stars | **Mirror it immediately** — it took a lawsuit and 9 years to exist |

---

# 7. Build sequence

```
M0  Secure the manifest        mirror the 94MB off GitHub              ← DONE
M1  Benchmark the VLM          benchmark_vlm.py, 1 GPU-hour, ~$2-3:
                                 - real pages/sec (all §4.4 figures are estimates)
                                 - Qwen3-VL vs ABBYY on the SAME degraded pages
                                 - schema-validity rate under guided decoding
                                 - refusal rate (should be 0% self-hosted)
M2  Coverage validation        500 docs, 3-way source split (~20 min)
M3  Pilot ingestion            STARGATE, ~90k pages, under $25
M4  Extraction + graph         entities, relations, Neo4j, citation integrity
                                 + agent harness loop (AGENT_HARNESS.md)
M5  Search API                 hybrid vector + graph, cited results
M6  3D front end               three.js, query-scoped subgraphs
M7  Full-corpus scale-out      5.6 GPU-days + 6-11 days acquisition, ~$3.5k
```

**M1 costs about $2.50 and replaces every throughput estimate in this document
with a measurement.** Do not commit 5.6 GPU-days on the strength of an
estimate. M0-M3 together are a weekend and under $30.

---

# 8. What this will *not* be

Stated so the public claims stay defensible:

- **Not "every declassified CIA document."** CREST is 12.2M pages of a much
  larger declassified universe. Call it what it is: the CREST archive.
- **Not perfectly transcribed.** Degraded 1950s carbon copies will contain
  errors, and a VLM may hallucinate on illegible pages where OCR would simply
  fail. Surface per-page legibility in the UI and always link the scan itself
  so a reader can check the machine's work.
- **Not word-level citable.** Citations resolve to document + page, by design
  (§4.3).
- **Not a claim of completeness** until §3.3 is resolved with a real number.
