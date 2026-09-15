# CIA CREST Knowledge Graph

Tooling to ingest the CIA's declassified **CREST** archive — 934,738 documents
/ 12,172,653 pages of scanned 1990s microfilm — into a citable, searchable
knowledge graph. Every answer links back to the page image it came from.

**Full corpus cost: ~$2,400**, self-hosted.

---

## What this is built from, and why

The naive approach to "search a document archive with AI" is to embed
everything into a vector database and retrieve by similarity. That fails here
for a specific reason, and the architecture is shaped around it.

### The four layers

| Layer | Tool | What it holds |
|---|---|---|
| **Page understanding** | Self-hosted Qwen3-VL (vision-language model) | Transcription, visual elements, and entities — one pass per page |
| **Semantic search** | Qdrant, 768-dim int8 | ~18M passage embeddings for "find me things about X" |
| **Relationships** | Neo4j | ~12M `MENTIONS` edges linking entities to the documents they appear in |
| **Provenance** | Object store | The original PDFs, so every claim resolves to a page image |

### Why a vision model instead of OCR

CREST pages are scanned microfilm that the Agency never OCR'd. Conventional OCR
would produce text — and silently discard the maps, photographs, stamps, seals,
redaction bars and handwritten marginalia that often carry the actual meaning.
A redaction bar is information. A routing stamp tells you who saw a document.

So vision runs over **all 12.1M pages**, not just the ones without a text
layer, and transcription, visual-element detection and entity extraction happen
in a single model call per page rather than as three separate stages.

### Why a graph *and* a vector store

This is the central design decision, and neither component alone is sufficient.

**Vector search answers "what is this about."** Embeddings find passages
semantically near a query, including paraphrases and synonyms. What they cannot
do is traverse. Ask "which other operations involved the people named in this
cable" and similarity search has no mechanism to answer — the question is about
*connections between* documents, not the content of any one of them.

**Graph traversal answers "what connects to what."** That is the actual
research question for a declassified archive: overlaps across reports, shared
personnel, chains of reference. But a graph cannot answer "find documents about
psychological conditioning" unless someone already tagged them that way.

The two are therefore chained rather than chosen between:

```
query ──► embed ──► Qdrant: top-k passages ──► document ids
                                                    │
                            Neo4j: entities in those documents,
                            and the other documents they appear in
                                                    │
                          cited results + a subgraph for the 3D view
```

Semantic search finds the entry points; the graph expands them into structure.

### Why relationships are computed, not stored

The obvious way to represent "these two entities are connected" is an explicit
edge between them. At this scale that is fatal: linking every entity pair on
every page produces **0.1–1.3 billion edges** depending on entity density, most
of them meaningless — two names on the same routing slip are not related in any
useful sense.

Instead only `(Entity)-[:MENTIONS]->(Document)` is stored, about **12M edges**,
and co-occurrence is derived at query time by a two-hop traversal. Cost is
bounded by one entity's degree rather than by the size of the graph.

A name appearing on six pages of one document is **one node**, with the page
numbers carried on the edge — so a result can say "mentioned 6 times, pages
3–19" and deep-link each one, without inflating the graph.

### Why int8 quantisation is not optional

```
18.3M vectors @ 1536d float32   168 GB   needs a large dedicated machine
18.3M vectors @  768d float32    84 GB   still expensive
18.3M vectors @  768d int8       21 GB   fits in RAM on a $60/mo VPS
```

Quantisation with rescoring costs a few points of recall and moves the entire
retrieval layer from "specialised infrastructure" to "one modest server."
Total hosting lands near **$214/month** against $300–700 for a managed vector
database alone.

### Why the 3D view is query-scoped

The graph contains roughly 3.2M entity nodes. Force-directed layout is
O(n log n) *per frame*, and beyond about 10,000 nodes a human is reading fog
rather than structure. Nothing renders until a search is made; each query
returns at most **~2,000 nodes**. Broad queries — "Soviet Union" touches
~180,000 documents — return a facet panel of co-occurring entities and a
timeline to drill through, rather than an unreadable hairball.

### Why self-hosted weights, not a hosted API

**Measured, not assumed:** a production API declined to extract structured data
from coup-operation material, stating it would refuse *"even if this were a
real declassified document."*

CREST is full of such material — PBSUCCESS, PBFORTUNE, ZRRIFLE, Phoenix,
MKUltra. Refusals cluster on exactly the documents researchers care about most,
and they arrive as HTTP 200 with prose, so they enter the pipeline as malformed
records rather than as errors. An archive that silently omits the Guatemala
coup while faithfully indexing cafeteria memos is worse than no archive.

Open weights on rented GPUs have no policy layer, are deterministic at
`temperature=0`, and cannot be deprecated mid-project. Full evidence in
[`PRD.md`](PRD.md) §4.2.

### Why an agent harness sits around it

12.1M pages will produce malformed output, hallucinated entities and refusal-like
failures at some rate. The pipeline is deterministic on the hot path; every
failure is quarantined as replayable JSON with the raw model output. A Hermes
agent then reads the accumulated failures **offline**, finds patterns, and
proposes prompt or schema fixes that a human merges.

Two hard rules: the agent never runs per-page (slow, costly, irreproducible),
and the agent never edits the validator — asked to reduce failures, an agent
relaxes the constraint instead of meeting it. See
[`AGENT_HARNESS.md`](AGENT_HARNESS.md).

### Why the cost is low

Self-hosted on rented GPUs (~$1,600 of compute) rather than billed per API
call, and the 934,738-row index already exists — mirrored in
[`manifest/`](manifest/) so it cannot disappear.

---

# Setup

Everything below assumes a **freshly rented GPU box** (RunPod, Vast.ai, Lambda
— $1.49–3.29/hr for an H100). Work through the parts in order.

---

## Part 1 — Install the Hermes agent harness

Do this first. The harness is what turns a one-off script run into a pipeline
that improves between batches.

### 1.1 Install

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

### 1.2 Run the setup wizard

```bash
hermes setup
```

Pick any provider you already have a key for — this model drives the *agent*
(reading failure reports, proposing fixes), not the page-transcription work.
A mid-tier model is plenty. The heavy lifting is done by the local VLM you
install in Part 2.

### 1.3 Verify

```bash
hermes doctor
```

Fix anything it flags before continuing. Then confirm the agent runs:

```bash
hermes chat -q "Reply with OK if you can run shell commands."
```

---

## Part 2 — Install and serve the vision model

This is the model that actually reads the 12.2M pages.

### 2.1 Install vLLM

```bash
python3 -m venv ~/vllm-env
source ~/vllm-env/bin/activate
pip install vllm
```

### 2.2 Download and serve Qwen3-VL

**Qwen3-VL** is the recommended model: OCR across 32 languages, explicitly
robust to low light, blur and tilt — which is exactly what 1950s microfilm
produces. OCRBench 896 vs Gemma-3's 480. Alternatives worth benchmarking:
InternVL3, Pixtral, Molmo.

Weights download automatically on first serve (~16 GB for the 8B):

```bash
vllm serve Qwen/Qwen3-VL-8B-Instruct \
    --port 8000 \
    --limit-mm-per-prompt image=1 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.90
```

Leave this running. Open a second shell for everything below.

> **Not Grok.** xAI's open weights (Grok-1, Grok-2) are text-only — no released
> multimodal weights, so they cannot read page images.

### 2.3 Verify the server

```bash
curl -s http://localhost:8000/v1/models | python3 -m json.tool
```

You should see `Qwen/Qwen3-VL-8B-Instruct` listed.

### 2.4 Point Hermes at the local model

So the agent can inspect pages itself when triaging failures:

```bash
hermes config set auxiliary.vision.provider openai
hermes config set auxiliary.vision.base_url http://localhost:8000/v1
hermes config set auxiliary.vision.model Qwen/Qwen3-VL-8B-Instruct
hermes config set auxiliary.vision.api_key local
```

Restart any running Hermes session for this to take effect.

Optionally drive the *agent itself* from the same local model — fully offline,
no API keys anywhere:

```bash
hermes config set model.provider openai
hermes config set model.base_url http://localhost:8000/v1
hermes config set model.default Qwen/Qwen3-VL-8B-Instruct
hermes config set model.api_key local
```

An 8B model is weak for the agent's reasoning work. Prefer a hosted model for
the agent and keep the local VLM for pages — the agent makes a few hundred
calls, the VLM makes 12 million.

---

## Part 3 — Install this repo and its skill

### 3.1 Clone

```bash
git clone https://github.com/MersivMedia/CIA-crest-knowledge-graph.git
cd CIA-crest-knowledge-graph
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3.2 Install the skill into Hermes

```bash
mkdir -p ~/.hermes/skills/research
cp -r skills/cia-crest-archive ~/.hermes/skills/research/
hermes skills list | grep crest
```

The skill carries everything learned building this: which sources work, which
are bot-walled, the measured cost model, and the traps (encoded filenames,
contaminated search tags, sample sizes that lie).

### 3.3 Verify the manifest

The full 934,738-row index ships **in this repo** under `manifest/` — 94 MB,
already cloned. Confirm it is intact:

```bash
cd manifest && sha256sum -c SHA256SUMS && cd ..
# crest1.zip: OK
# crest2.zip: OK
```

Upstream original: [`morisy/ci-trend-explorer`](https://github.com/morisy/ci-trend-explorer)
(3 stars, unmaintained since 2017). Mirrored here because a nine-year lawsuit
should not depend on one personal account staying online. See
[`manifest/README.md`](manifest/README.md).

### 3.4 Build the index

```bash
python manifest_to_sqlite.py --data-dir manifest
```

```
  documents : 934,738
  pages     : 12,172,653
  largest   : 3,167 pages
  db        : ~/.hermes/data/crest-archive/crest.db (498 MB)
```

### 3.5 Verify document fetching

```bash
python fetch_document.py CIA-RDP80T00294A001200090027-4
# source : archive.org   ocr : abbyy   pages : 8   words pg0 : 503
```

And one archive.org lacks, to confirm the Wayback fallback:

```bash
python fetch_document.py CIA-RDP55-00166A000200050081-9
# source : wayback   ocr : required
```

---

## Part 4 — Benchmark before spending GPU-days

```bash
python benchmark_vlm.py \
    --endpoint http://localhost:8000/v1 \
    --model Qwen/Qwen3-VL-8B-Instruct \
    --n 50
```

Costs about one GPU-hour. Answers the four questions every estimate in
`PRD.md` depends on:

```
valid schema     47  (94%)
bad schema        3  ( 6%)
REFUSED           0  ( 0%)        ← must be 0 self-hosted
median latency   2.10s/page  (0.48 pages/sec, 1 stream)
  at 32 concurrent:   15.2 pg/s -> 222 h for full corpus
ABBYY agreement  median 0.812
mean visual_elements/page 2.3     ← what pure OCR would have missed
```

**Do not skip this.** Every throughput figure in the PRD is an estimate until
you run it. If `visual_elements` comes back near zero, vision is not earning
its cost on these documents and the architecture needs rethinking.

---

## Part 5 — Pilot, then scale

Start with one program (STARGATE, ~90,000 pages, under $25) rather than 12.2
million. The architecture is identical; the corpus is a swappable input.

Then check what you can actually obtain:

```bash
python coverage_audit.py --n 500 --delay 0.4
```

~20 minutes of paced requests. Reports what fraction is on archive.org, what
only Wayback has, and what neither holds. **This determines what you can
honestly claim about completeness.** Current small samples disagree badly
(23% / 55% / 75% / 88%) — see `PRD.md` §3.3.

---

## Part 6 — Run the learning loop

The pipeline is deterministic code. The agent is a separate offline loop that
reads failures and proposes improvements. Full detail in
[`AGENT_HARNESS.md`](AGENT_HARNESS.md).

### 6.1 Process a batch, then look at what failed

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

`ungrounded` is the one to watch — an entity absent from the transcription is a
plausible fabrication carrying a real citation.

### 6.2 Hand the failures to the agent

```bash
hermes
```

```
Load the cia-crest-archive skill and read AGENT_HARNESS.md in this repo.
Then run Task 1 — failure triage.
```

The agent reads the report, inspects raw model output from
`~/.hermes/data/crest-archive/quarantine/`, and reports the root cause.

### 6.3 Approve a fix, then make the agent prove it

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
compare rather than assume.

### 6.4 Optional — schedule it

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

## What is here

| Path | Purpose |
|---|---|
| `manifest/` | **The full 934,738-row CREST index, mirrored** (94 MB) |
| `PRD.md` | Product requirements + full cost analysis, every figure sourced |
| `AGENT_HARNESS.md` | The learning loop — five copy-paste agent prompts |
| `schema.py` | JSON contract, prompt, validator, hallucination check |
| `manifest_to_sqlite.py` | Manifest → indexed SQLite |
| `fetch_document.py` | Fetch one document, preferring already-OCR'd sources |
| `coverage_audit.py` | Measure the three-way source split |
| `benchmark_vlm.py` | Benchmark a VLM before committing GPU-days |
| `ingest.py` | Hot-path processing + quarantine + failure reporting |
| `skills/` | The Hermes skill, installable with one `cp` |

---

## Key findings (so you don't repeat the work)

**The full manifest already exists.** `morisy/ci-trend-explorer` — Michael
Morisy founded MuckRock, whose lawsuit forced the CREST release. 94 MB,
934,738 rows, every field populated. Mirrored here.

**cia.gov is unusable programmatically.** Akamai Bot Manager returns a
`bm-verify` challenge to every scripted request. The Wayback Machine's `id_`
raw-content path serves the same PDFs without the wall.

**Existing OCR exists but we do not rely on it.** archive.org holds 275,008
CREST items with ABBYY OCR and per-word bounding boxes (`_djvu.xml`), free — a
useful *reference* for benchmarking transcription quality. But it is text-only
and covers an unresolved 23–88% of the corpus.

**Wayback CREST PDFs have no text layer** (0 of 15 tested). Don't confuse them
with `/readingroom/docs/` captures, which do — that's a different corpus.

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
