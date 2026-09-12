# CREST Knowledge Graph

Tooling to ingest the CIA's declassified **CREST** archive — 934,738 documents
/ 12,172,653 pages — into a citable, searchable knowledge graph, using a
self-hosted vision-language model and a Hermes agent harness that learns from
its own failures.

**Full corpus cost: ~$2,400.** Most of the expensive part is already done by
someone else; this repo is mostly about finding it and not re-paying for it.

---

## What is here

| File | Purpose |
|---|---|
| `PRD.md` | Product requirements + full cost analysis, every figure sourced |
| `AGENT_HARNESS.md` | How to run the learning loop in Hermes (copy-paste prompts) |
| `schema.py` | JSON contract, prompt, validator, hallucination check |
| `manifest_to_sqlite.py` | 934,738-row manifest → indexed SQLite |
| `fetch_document.py` | Fetch one document, preferring already-OCR'd sources |
| `coverage_audit.py` | Measure the three-way source split |
| `benchmark_vlm.py` | Benchmark a VLM before committing GPU-days |
| `ingest.py` | Hot-path processing + quarantine + failure reporting |

---

## Key findings (so you don't repeat the work)

**The full manifest already exists.** `morisy/ci-trend-explorer` on GitHub —
Michael Morisy founded MuckRock, whose lawsuit forced the CREST release. 94 MB,
934,738 rows, every field populated. Mirror it; it hangs off one unmaintained
account with 3 stars.

**Much of the OCR already exists.** archive.org holds 275,008 CREST items with
ABBYY OCR *and per-word bounding boxes* (`_djvu.xml`). Free.

**cia.gov is unusable programmatically.** Akamai Bot Manager returns a
`bm-verify` challenge to every scripted request. The Wayback Machine's `id_`
raw-content path serves the same PDFs without the wall.

**Hosted APIs refuse this content.** Measured: a production API refused to
extract structured data from coup-operation material, explicitly stating that
it would refuse *"even if this were a real declassified document."* CREST is
full of such material. **Self-host open weights** — see `PRD.md` §4.2.

**Wayback CREST PDFs have no text layer** (0 of 15 tested). Don't confuse them
with the `/readingroom/docs/` captures, which do — that's a different corpus.

---

## Setup

### Prerequisites

- Python 3.10+
- ~4 TB storage for the full corpus (a pilot needs ~5 GB)
- A GPU for the VLM stage — rented is fine (see step 5)

### 1. Clone and install

```bash
git clone https://github.com/MersivMedia/crest-knowledge-graph.git
cd crest-knowledge-graph
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get the manifest (94 MB)

```bash
mkdir -p ~/.hermes/data/crest-archive
cd ~/.hermes/data/crest-archive
curl -sL -o crest1.zip "https://raw.githubusercontent.com/morisy/ci-trend-explorer/master/Crest%201.zip"
curl -sL -o crest2.zip "https://raw.githubusercontent.com/morisy/ci-trend-explorer/master/Crest%202.zip"
sha256sum crest*.zip > SHA256SUMS
cd -
```

### 3. Build the index

```bash
python manifest_to_sqlite.py
```

Expected:

```
  documents : 934,738
  pages     : 12,172,653
  largest   : 3,167 pages
  db        : ~/.hermes/data/crest-archive/crest.db (498 MB)
```

### 4. Verify document fetching

```bash
python fetch_document.py CIA-RDP80T00294A001200090027-4
```

Expected — text *and* word-level bounding boxes, at no cost:

```
doc      : CIA-RDP80T00294A001200090027-4
source   : archive.org
ocr      : abbyy
pages    : 8
words pg0: 503
```

Try one archive.org lacks, to confirm the Wayback fallback:

```bash
python fetch_document.py CIA-RDP55-00166A000200050081-9
# source : wayback   ocr : required
```

### 5. Stand up the VLM

Rent a GPU (RunPod, Vast.ai, Lambda — $1.49–3.29/hr for an H100), then:

```bash
pip install vllm
vllm serve Qwen/Qwen3-VL-8B-Instruct \
    --port 8000 \
    --limit-mm-per-prompt image=1 \
    --max-model-len 8192
```

Confirm it is up:

```bash
curl -s http://localhost:8000/v1/models | python3 -m json.tool
```

### 6. Benchmark before spending GPU-days

```bash
python benchmark_vlm.py \
    --endpoint http://localhost:8000/v1 \
    --model Qwen/Qwen3-VL-8B-Instruct \
    --n 50
```

This costs about one GPU-hour and answers the four questions every estimate in
`PRD.md` depends on:

```
valid schema     47  (94%)
bad schema        3  ( 6%)
REFUSED           0  ( 0%)        ← should be 0 self-hosted
median latency   2.10s/page  (0.48 pages/sec, 1 stream)
  at 32 concurrent:   15.2 pg/s -> 222 h for full corpus
ABBYY agreement  median 0.812
mean visual_elements/page 2.3     ← what pure OCR would have missed
```

**Do not skip this.** Every throughput figure in the PRD is an estimate until
you run it. If ABBYY agreement is high and visual_elements is near zero, vision
is not earning its cost on your documents.

### 7. Pilot on one program

Start with STARGATE (~90,000 pages, under $25) rather than 12.2 million. The
architecture is identical; the corpus is a swappable input.

### 8. Check source coverage

```bash
python coverage_audit.py --n 500 --delay 0.4
```

~20 minutes of paced requests. Reports what fraction is on archive.org, what
only Wayback has, and what neither holds. **This determines what you can
honestly claim about completeness.** Current small samples disagree badly
(23% / 55% / 75% / 88%) — see `PRD.md` §3.3.

---

## Running with the Hermes agent harness

The pipeline is deterministic code. The agent is a separate offline loop that
reads failures and proposes improvements. Full detail in `AGENT_HARNESS.md`.

### 1. Install Hermes

```bash
pip install hermes-agent     # or see https://hermes-agent.nousresearch.com/docs
hermes setup
```

### 2. Install the skill

```bash
mkdir -p ~/.hermes/skills/research
cp -r skills/cia-crest-archive ~/.hermes/skills/research/
```

Verify it loads:

```bash
hermes
> What skills do you have for CREST?
```

### 3. Process a batch, then look at what failed

```bash
python ingest.py --report
```

```
status breakdown
  ok                    11,204  ( 94.2%)
  schema_invalid           402  (  3.4%)
  ungrounded               210  (  1.8%)
  unparseable               78  (  0.7%)

most common failure details (the agent's actual work queue)
  [schema_invalid]
      312x  visual_elements[0].type invalid: 'watermark'
```

### 4. Hand the failures to the agent

```bash
hermes
```

```
Load the cia-crest-archive skill and read AGENT_HARNESS.md in this repo.
Then run Task 1 — failure triage.
```

The agent reads the report, inspects raw model output from
`~/.hermes/data/crest-archive/quarantine/`, and tells you the root cause.

### 5. Approve a fix, then make the agent prove it

```
Run Task 2 — propose a prompt fix for the largest failure category.
```

After you approve the diff:

```
Run Task 3 — prove it with --replay-all.
```

```bash
python ingest.py --replay-all
# 287/402 previously-failing pages now pass
```

Prompt versions are hashed, so `--report` shows success rate per version. You
compare, rather than assume.

### 6. Optional — schedule it

```
Create a cron job for every Monday 9am: run the CREST failure analysis cycle
from AGENT_HARNESS.md and report findings. Do not modify files; report only.
```

---

## The three rules of the harness

1. **The agent never edits the validator.** Asked to reduce failures, an agent
   relaxes the constraint rather than meeting it. `validate_page()` and
   `PAGE_SCHEMA` are the contract.
2. **Fixes are proven with `--replay-all`.** The quarantine corpus is a
   regression suite. Assertion is not evidence.
3. **A human merges.** The agent opens a diff with before/after numbers.

---

## Cost summary

| Line | Cost |
|---|---|
| Manifest + acquisition (6–11 days bandwidth) | $0 |
| One-pass VLM, self-hosted, 12.2M pages | $1,612 |
| Embeddings | $292 |
| Storage, 6 months | $504 |
| **Full corpus, one-time** | **~$2,408** |
| Pilot (STARGATE, 90k pages) | **under $25** |
| Benchmark (1 GPU-hour) | **~$2–3** |

Recurring: $125–215/mo. Full derivation in `PRD.md` §5.

---

## What this is not

- **Not "every declassified CIA document."** CREST is 12.2M pages of a much
  larger declassified universe. Call it what it is.
- **Not perfectly transcribed.** Degraded 1950s carbon copies contain errors,
  and a VLM may hallucinate where OCR would visibly fail. Surface per-page
  legibility and always link the scan.
- **Not word-level citable** by design. Citations resolve to document + page.
- **Not a completeness claim** until `coverage_audit.py` is run at n≥500.

## License

MIT. The underlying documents are US government works in the public domain.
