#!/usr/bin/env python3
"""
The learning loop: a deterministic pipeline that records its own failures so a
Hermes agent can improve it between runs.

THE BOUNDARY (this is the important design decision)

    HOT PATH  — deterministic, no agent. 12.2M pages must be processed by code
                whose cost and latency are predictable. An LLM agent in this
                loop would be slow, expensive and non-reproducible.

    COLD PATH — the agent. It reads quarantined failures, finds patterns,
                proposes prompt/schema/parser changes, and tests them against
                the recorded failure corpus.

    GATE      — a human. The agent never edits the validator and never
                auto-merges. It opens a diff with evidence; you decide.

This file implements the hot path and the quarantine that feeds the cold path.
The agent-facing side is driven by the prompts in AGENT_HARNESS.md.

Usage:
    python ingest.py --limit 100                    # process a batch
    python ingest.py --report                       # what is failing, and how
    python ingest.py --replay quarantine/xyz.json   # re-test after a fix
"""
import argparse, hashlib, json, os, sqlite3, sys, time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema import (EXTRACTION_PROMPT, PAGE_SCHEMA, validate_page,
                    check_grounding, extract_json)

DATA = os.path.expanduser("~/.hermes/data/crest-archive")
DB = os.path.join(DATA, "crest.db")
STATE = os.path.join(DATA, "ingest.db")
QUARANTINE = os.path.join(DATA, "quarantine")


# ---------------------------------------------------------------- state
def init_state():
    os.makedirs(QUARANTINE, exist_ok=True)
    con = sqlite3.connect(STATE)
    con.execute("""CREATE TABLE IF NOT EXISTS pages (
        doc_id TEXT, page INTEGER, status TEXT, legibility REAL,
        entities INTEGER, visual_elements INTEGER, ungrounded INTEGER,
        prompt_hash TEXT, model TEXT, seconds REAL, ts REAL,
        PRIMARY KEY (doc_id, page))""")
    con.execute("""CREATE TABLE IF NOT EXISTS failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id TEXT, page INTEGER, reason TEXT, detail TEXT,
        raw_path TEXT, prompt_hash TEXT, model TEXT, ts REAL,
        resolved INTEGER DEFAULT 0)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_status ON pages(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reason ON failures(reason, resolved)")
    con.commit()
    return con


def prompt_hash():
    """Version the prompt. When the agent changes it, we can measure whether
    failure rates actually improved rather than assuming they did."""
    return hashlib.sha256(EXTRACTION_PROMPT.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- hot path
def process_page(raw_text, doc_id, page, model, seconds, con):
    """Deterministic. Classify, validate, record. No agent, no retries here."""
    ph = prompt_hash()
    obj = extract_json(raw_text)

    if obj is None:
        return quarantine(con, doc_id, page, "unparseable",
                          "model did not return recoverable JSON",
                          raw_text, ph, model)

    ok, errs = validate_page(obj)
    if not ok:
        return quarantine(con, doc_id, page, "schema_invalid",
                          "; ".join(errs[:5]), raw_text, ph, model)

    grounded, ungrounded = check_grounding(obj)
    status = "ok" if grounded else "ungrounded"
    if not grounded:
        quarantine(con, doc_id, page, "ungrounded_entities",
                   f"not found in transcription: {ungrounded[:8]}",
                   raw_text, ph, model, keep_record=True)

    con.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, page, status, obj.get("legibility"),
                 len(obj.get("entities") or []),
                 len(obj.get("visual_elements") or []),
                 len(ungrounded), ph, model, seconds, time.time()))
    con.commit()
    return status, obj


def quarantine(con, doc_id, page, reason, detail, raw, ph, model, keep_record=False):
    """Failures are DATA, not noise. Every one is replayable against a fix."""
    path = os.path.join(QUARANTINE, f"{doc_id}_p{page}_{reason}.json")
    json.dump({"doc_id": doc_id, "page": page, "reason": reason,
               "detail": detail, "raw": raw, "prompt_hash": ph, "model": model},
              open(path, "w"), indent=1)
    con.execute(
        "INSERT INTO failures (doc_id,page,reason,detail,raw_path,prompt_hash,model,ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, page, reason, detail, path, ph, model, time.time()))
    if not keep_record:
        con.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, page, reason, None, 0, 0, 0, ph, model, 0, time.time()))
    con.commit()
    return reason, None


# ---------------------------------------------------------------- cold path
def report(con):
    """What the agent reads at the start of an improvement cycle."""
    total = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    if not total:
        print("no pages processed yet — run --limit N first")
        return

    print(f"PROCESSED: {total:,} pages\n")
    print("status breakdown")
    for st, n in con.execute(
            "SELECT status, COUNT(*) FROM pages GROUP BY status ORDER BY 2 DESC"):
        print(f"  {st:<22} {n:>8,}  ({n/total*100:>5.1f}%)")

    rows = con.execute(
        "SELECT reason, COUNT(*) FROM failures WHERE resolved=0 "
        "GROUP BY reason ORDER BY 2 DESC").fetchall()
    if rows:
        print("\nopen failures by reason")
        for reason, n in rows:
            print(f"  {reason:<22} {n:>8,}")

        print("\nmost common failure details (the agent's actual work queue)")
        for reason, _ in rows[:3]:
            details = [d for (d,) in con.execute(
                "SELECT detail FROM failures WHERE reason=? AND resolved=0 LIMIT 400",
                (reason,))]
            print(f"\n  [{reason}]")
            for detail, n in Counter(details).most_common(5):
                print(f"    {n:>5}x  {detail[:78]}")

    print("\nlegibility distribution (low scores = candidates for re-read)")
    for lo, hi in ((0.0, 0.3), (0.3, 0.6), (0.6, 0.8), (0.8, 1.01)):
        n = con.execute(
            "SELECT COUNT(*) FROM pages WHERE legibility>=? AND legibility<?",
            (lo, hi)).fetchone()[0]
        print(f"  {lo:.1f}-{hi:.1f}   {n:>8,}")

    print("\nprompt versions in play")
    for ph, n, ok in con.execute(
            "SELECT prompt_hash, COUNT(*), SUM(status='ok') FROM pages "
            "GROUP BY prompt_hash ORDER BY 2 DESC"):
        rate = (ok or 0) / n * 100
        print(f"  {ph}  {n:>8,} pages  {rate:>5.1f}% ok")
    print("\n  ^ compare success rates ACROSS prompt versions before keeping a change.")


def replay(path):
    """Re-run the current validator against an old failure. This is how the
    agent proves a fix works instead of asserting it."""
    rec = json.load(open(path))
    obj = extract_json(rec["raw"])
    if obj is None:
        print(f"STILL FAILS (unparseable): {rec['doc_id']} p{rec['page']}")
        return False
    ok, errs = validate_page(obj)
    grounded, ung = check_grounding(obj)
    if ok and grounded:
        print(f"NOW PASSES: {rec['doc_id']} p{rec['page']}")
        return True
    print(f"STILL FAILS: {rec['doc_id']} p{rec['page']}")
    for e in errs[:4]:
        print(f"  - {e}")
    if ung:
        print(f"  - ungrounded: {ung[:5]}")
    return False


def replay_all(con):
    rows = con.execute(
        "SELECT id, raw_path FROM failures WHERE resolved=0").fetchall()
    fixed = 0
    for fid, path in rows:
        if os.path.exists(path) and replay(path):
            con.execute("UPDATE failures SET resolved=1 WHERE id=?", (fid,))
            fixed += 1
    con.commit()
    print(f"\n{fixed}/{len(rows)} previously-failing pages now pass")
    return fixed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--replay")
    ap.add_argument("--replay-all", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    con = init_state()
    if a.report:
        report(con)
    elif a.replay:
        replay(a.replay)
    elif a.replay_all:
        replay_all(con)
    else:
        print("Nothing to do. Try --report, --replay-all, or see README.md")
        print(f"prompt version currently: {prompt_hash()}")
