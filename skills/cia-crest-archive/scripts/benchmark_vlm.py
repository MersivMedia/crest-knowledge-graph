#!/usr/bin/env python3
"""
Benchmark a VLM on real CREST pages before committing GPU-days.

Measures the four things every cost estimate in the PRD depends on:

  1. THROUGHPUT      real pages/sec (all PRD figures are estimates)
  2. QUALITY         VLM transcription vs archive.org's ABBYY on the SAME pages
  3. SCHEMA          how often the model returns valid, parseable JSON
  4. REFUSAL         how often a hosted API declines declassified content
                     (self-hosted open weights should be 0%)

Run against either a local vLLM server or a hosted API. The point is to replace
estimates with measurements on YOUR hardware and YOUR documents.

    # local vLLM (recommended - no content filter)
    python benchmark_vlm.py --endpoint http://localhost:8000/v1 \
        --model Qwen/Qwen3-VL-8B-Instruct --n 50

    # hosted API, to measure the refusal rate
    python benchmark_vlm.py --endpoint https://openrouter.ai/api/v1 \
        --model qwen/qwen3-vl-8b-instruct --api-key $OPENROUTER_API_KEY --n 50

Outputs benchmark_results.json plus a summary table.
"""
import argparse, base64, io, json, os, re, sqlite3, statistics, subprocess
import sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema import PAGE_SCHEMA, EXTRACTION_PROMPT, validate_page

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
DEFAULT_DB = os.path.expanduser("~/.hermes/data/crest-archive/crest.db")

REFUSAL_MARKERS = (
    "i can't", "i cannot", "i won't", "i'm not able", "unable to assist",
    "can't help", "cannot help", "i'd be happy to discuss", "inappropriate",
)


# ---------------------------------------------------------------- fetching
def curl(url, out=None, timeout=90):
    cmd = ["curl", "-sL", "--compressed", "--max-time", str(timeout), "-A", UA]
    cmd += ["-o", out] if out else ["-o", "-"]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 20)
        return r.stdout if not out else b""
    except Exception:
        return b""


def ia_meta(doc_id):
    body = curl(f"https://archive.org/metadata/{doc_id}").decode("utf-8", "replace")
    if len(body) < 5 or body.strip() == "{}":
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def ia_url(meta, fmt):
    """Direct node URL for a derivative. Filenames contain spaces/commas -> MUST encode."""
    fn = next((f["name"] for f in meta.get("files", []) if f.get("format") == fmt), None)
    return f"https://{meta['server']}{meta['dir']}/{urllib.parse.quote(fn)}" if fn else None


def get_page_image(doc_id, meta, page_no, workdir):
    """Render page N of the item's PDF to a JPEG. Returns path or None."""
    pdf_url = ia_url(meta, "Image Container PDF") or ia_url(meta, "Text PDF")
    if not pdf_url:
        return None
    pdf_path = os.path.join(workdir, f"{doc_id}.pdf")
    if not os.path.exists(pdf_path):
        curl(pdf_url, out=pdf_path, timeout=180)
    try:
        import pymupdf
        doc = pymupdf.open(pdf_path)
        if page_no >= doc.page_count:
            return None
        pix = doc[page_no].get_pixmap(dpi=150)
        img_path = os.path.join(workdir, f"{doc_id}_p{page_no}.jpg")
        pix.save(img_path)
        return img_path
    except Exception:
        return None


def abbyy_text(meta, page_no):
    """Reference transcription from archive.org's ABBYY OCR, for the same page."""
    url = ia_url(meta, "Djvu XML")
    if not url:
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(curl(url, timeout=180).decode("utf-8", "replace"))
    except Exception:
        return None
    for i, obj in enumerate(root.iter("OBJECT")):
        if i == page_no:
            return " ".join(w.text for w in obj.iter("WORD") if w.text)
    return None


# ---------------------------------------------------------------- inference
def call_vlm(endpoint, model, api_key, img_path, timeout=180):
    """OpenAI-compatible chat/completions with an image. Works with vLLM and hosted APIs."""
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "max_tokens": 1500,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": EXTRACTION_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)[:160], "seconds": time.time() - t0}
    txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    usage = d.get("usage") or {}
    return {"text": txt, "seconds": time.time() - t0,
            "out_tokens": usage.get("completion_tokens"),
            "in_tokens": usage.get("prompt_tokens")}


# ---------------------------------------------------------------- scoring
def word_overlap(a, b):
    """Crude transcription agreement: Jaccard over lowercased word sets."""
    wa = set(re.findall(r"[a-z]{3,}", (a or "").lower()))
    wb = set(re.findall(r"[a-z]{3,}", (b or "").lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def extract_json(text):
    """Models wrap JSON in prose or code fences. Recover it."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else None
    if not cand:
        i, j = text.find("{"), text.rfind("}")
        cand = text[i:j + 1] if i >= 0 and j > i else None
    if not cand:
        return None
    try:
        return json.loads(cand)
    except Exception:
        return None


def looks_like_refusal(text):
    low = (text or "").lower()
    return any(mk in low for mk in REFUSAL_MARKERS) and len(low) < 2000


# ---------------------------------------------------------------- main
def run(args):
    con = sqlite3.connect(args.db)
    rows = con.execute(
        "SELECT document_number, pages FROM documents "
        "WHERE pages BETWEEN 1 AND 20 ORDER BY RANDOM() LIMIT ?",
        (args.n * 3,)).fetchall()

    os.makedirs(args.workdir, exist_ok=True)
    results, done = [], 0

    for doc_id, npages in rows:
        if done >= args.n:
            break
        meta = ia_meta(doc_id)
        if not meta:
            continue
        img = get_page_image(doc_id, meta, 0, args.workdir)
        if not img:
            continue

        r = call_vlm(args.endpoint, args.model, args.api_key, img)
        if "error" in r:
            print(f"  ERR  {doc_id[:34]}  {r['error'][:60]}")
            results.append({"doc": doc_id, "status": "api_error", "error": r["error"]})
            done += 1
            continue

        parsed = extract_json(r["text"])
        refused = looks_like_refusal(r["text"]) and parsed is None
        valid, errs = (validate_page(parsed) if parsed else (False, ["no json"]))
        ref = abbyy_text(meta, 0)
        overlap = word_overlap(
            (parsed or {}).get("transcription", ""), ref) if (parsed and ref) else None

        status = "refused" if refused else ("ok" if valid else "bad_schema")
        results.append({
            "doc": doc_id, "status": status, "seconds": round(r["seconds"], 2),
            "out_tokens": r.get("out_tokens"), "schema_errors": errs[:3],
            "abbyy_overlap": round(overlap, 3) if overlap is not None else None,
            "entities": len((parsed or {}).get("entities") or []),
            "visual_elements": len((parsed or {}).get("visual_elements") or []),
        })
        done += 1
        mark = {"ok": "OK ", "refused": "REF", "bad_schema": "BAD"}[status]
        ov = f"{overlap:.2f}" if overlap is not None else "  - "
        print(f"  {mark}  {doc_id[:34]:<36} {r['seconds']:>5.1f}s  abbyy_overlap={ov}")

    # ---- summary
    ok = [x for x in results if x["status"] == "ok"]
    refused = [x for x in results if x["status"] == "refused"]
    bad = [x for x in results if x["status"] == "bad_schema"]
    errs = [x for x in results if x["status"] == "api_error"]
    secs = [x["seconds"] for x in results if x.get("seconds")]
    ovs = [x["abbyy_overlap"] for x in results if x.get("abbyy_overlap") is not None]

    n = len(results) or 1
    print("\n" + "=" * 66)
    print(f"BENCHMARK  model={args.model}  n={len(results)}")
    print("=" * 66)
    print(f"  valid schema     {len(ok):>4}  ({len(ok)/n*100:.0f}%)")
    print(f"  bad schema       {len(bad):>4}  ({len(bad)/n*100:.0f}%)")
    print(f"  REFUSED          {len(refused):>4}  ({len(refused)/n*100:.0f}%)")
    print(f"  api errors       {len(errs):>4}")
    if secs:
        med = statistics.median(secs)
        print(f"\n  median latency   {med:.2f}s/page   ({1/med:.2f} pages/sec, 1 stream)")
        for c in (8, 32, 64):
            pps = c / med
            hours = 12_172_653 / pps / 3600
            print(f"    at {c:>2} concurrent: {pps:>6.1f} pg/s -> {hours:>6,.0f} h for full corpus")
    if ovs:
        print(f"\n  ABBYY agreement  median {statistics.median(ovs):.3f}  "
              f"mean {statistics.mean(ovs):.3f}  n={len(ovs)}")
        print("    (1.0 = identical wordsets; <0.5 suggests one source is much better)")
    if ok:
        print(f"\n  mean entities/page        {statistics.mean([x['entities'] for x in ok]):.1f}")
        print(f"  mean visual_elements/page {statistics.mean([x['visual_elements'] for x in ok]):.1f}")
    if refused:
        print(f"\n  !! {len(refused)} REFUSALS — a hosted filter is redacting your archive.")
        print("     Self-host open weights; see PRD 4.2.")

    json.dump({"model": args.model, "endpoint": args.endpoint, "results": results},
              open(args.out, "w"), indent=1)
    print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8000/v1")
    ap.add_argument("--model", default="Qwen/Qwen3-VL-8B-Instruct")
    ap.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY", ""))
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--workdir", default="/tmp/crest_bench")
    ap.add_argument("--out", default="benchmark_results.json")
    run(ap.parse_args())
