# Running this pipeline inside a Hermes agent harness

The pipeline is deterministic code. The agent is a separate, offline loop that
reads its failures and proposes improvements. This document defines that
boundary and gives you the exact prompts.

---

## The boundary

```
┌─ HOT PATH ─────────────────────────────────────────────┐
│  12.2M pages · deterministic · no agent                │
│  fetch → VLM (guided JSON) → validate → graph/vectors  │
│  cost and latency must be predictable and reproducible │
└────────────────────────────────────────────────────────┘
                         │ failures
                         ▼
┌─ QUARANTINE ───────────────────────────────────────────┐
│  every failure kept as replayable JSON + the raw       │
│  model output that produced it                         │
└────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─ COLD PATH (the agent) ────────────────────────────────┐
│  reads the failure report, finds patterns, proposes    │
│  changes, PROVES them with --replay-all                │
└────────────────────────────────────────────────────────┘
                         │ diff + evidence
                         ▼
┌─ GATE (you) ───────────────────────────────────────────┐
│  human review. Nothing auto-merges.                    │
└────────────────────────────────────────────────────────┘
```

**Why the agent is not in the hot path.** An LLM agent deciding per page is
slow, costs unpredictably, and cannot be reproduced. Twelve million pages need
code whose behaviour you can predict and re-run identically. The agent's value
is in the *failures* — the 2-5% that need a human-grade judgement call — not
in the 95% that already work.

## Three rules

1. **The agent never edits `schema.py`'s validator.** An agent asked to reduce
   failures will relax the constraint rather than meet it. The validator is the
   contract; changing it silently redefines success.
2. **The agent must prove fixes with `--replay-all`.** "This should fix it" is
   not evidence. The quarantine corpus is a regression suite.
3. **A human merges.** The agent opens a diff with before/after numbers.

---

## Setup

```bash
hermes                              # start the agent
```

Point it at the repo and load the skill:

```
Load the cia-crest-archive skill and read AGENT_HARNESS.md in this repo.
The pipeline is in this directory. State lives in ~/.hermes/data/crest-archive/.
```

Optional — run improvement cycles on a schedule:

```
Create a cron job that runs every Monday at 9am: run the CREST failure
analysis cycle described in AGENT_HARNESS.md and report what it found.
Do not modify any files; report only.
```

---

## Task 1 — Failure triage (run this first, every cycle)

```
Analyse the current CREST ingestion failures.

1. Run: python ingest.py --report
2. For the largest failure category, read 10 quarantined files from
   ~/.hermes/data/crest-archive/quarantine/ and look at the RAW model output,
   not just the error message.
3. Tell me:
   - what the model actually did wrong (quote the raw output)
   - whether it is a prompt problem, a parser problem, or a genuinely
     illegible page
   - how many of the open failures share this root cause

Do NOT change any files yet. Report only.
```

## Task 2 — Propose a prompt fix

```
Based on the triage, propose a change to EXTRACTION_PROMPT in schema.py.

Rules:
- Do NOT modify validate_page() or PAGE_SCHEMA. The contract does not move.
- Show me the exact diff.
- Explain which failure category it targets and why you expect it to help.
- Predict the improvement as a number before we test it.

Then wait for my approval.
```

## Task 3 — Prove it

```
After I approve the prompt change:

1. Run: python ingest.py --replay-all
2. Report how many previously-failing pages now pass.
3. Compare against your prediction from Task 2. If you were wrong, say so
   plainly and explain what you misdiagnosed.
4. Run: python ingest.py --report and show the prompt-version comparison
   table so we can see whether the new prompt_hash is genuinely better.

If the fix made things worse, revert it and say why.
```

## Task 4 — Legibility audit (catches hallucination)

```
Check whether the model is inventing text on unreadable pages.

1. Query the pages table for records with legibility < 0.3 but a
   transcription longer than 200 characters. That combination is suspicious:
   the model said it could not read the page, then produced a lot of text.
2. For 5 of them, fetch the actual page image and compare.
3. Report the hallucination rate.

This is the most important quality check in the project. A fabricated sentence
attributed to a real CIA document is a serious defect — worse than a missing
page, because it is invisible to the reader.
```

## Task 5 — Coverage gap analysis

```
Run: python coverage_audit.py --n 500 --delay 0.4

Report the three-way split (archive.org / Wayback-only / neither) by PAGE
count, not document count. Then tell me:
- what fraction of the corpus we cannot obtain at all
- whether the gap clusters in particular RDP series or date ranges
- whether our public completeness claim needs revising
```

---

## What the agent should never do

| Never | Why |
|---|---|
| Edit `validate_page()` or `PAGE_SCHEMA` | It will relax constraints instead of meeting them |
| Auto-merge its own changes | No feedback loop without a human gate |
| Delete quarantined failures | They are the regression suite |
| "Fix" a page by editing its extracted data | Fix the process, never the record |
| Claim improvement without `--replay-all` | Assertion is not evidence |
| Re-run the full corpus to test a prompt | Use the quarantine corpus; it is free |

---

## Reading the report

```
status breakdown
  ok                      2  ( 40.0%)     ← passed schema + grounding
  schema_invalid          1  ( 20.0%)     ← malformed output; prompt/grammar issue
  ungrounded              1  ( 20.0%)     ← entity not in transcription = hallucination
  unparseable             1  ( 20.0%)     ← no recoverable JSON; use guided decoding

prompt versions in play
  f9a2a644b535   120,400 pages   94.2% ok
  a31b0c9e7721    80,100 pages   91.8% ok
  ^ compare success rates ACROSS prompt versions before keeping a change.
```

`ungrounded` is the one to watch. Schema errors are noisy but harmless — the
record is rejected. An ungrounded entity is a *plausible-looking* fabrication
that would otherwise enter the knowledge graph with a citation attached.

---

## Structured output: prompting vs guarantees

Prompting for JSON is a request. Constrained decoding is a guarantee.

```python
# vLLM — the model cannot emit a token that breaks the schema
payload = {"model": MODEL, "messages": [...],
           "guided_json": PAGE_SCHEMA, "temperature": 0}
```

With guided decoding, `unparseable` and `schema_invalid` should approach zero,
leaving the agent to work on the interesting failures: hallucination,
illegibility, and missed visual elements.

Keep `validate_page()` active regardless. **Guided decoding guarantees shape,
not truth.**
