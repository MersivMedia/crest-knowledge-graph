#!/usr/bin/env python3
"""
The contract between the model and the pipeline.

A VLM asked for JSON will *usually* return JSON. "Usually" is not a contract at
12.2 million pages. This module is the single source of truth for:

  * PAGE_SCHEMA        the JSON Schema every page record must satisfy
  * EXTRACTION_PROMPT  the prompt, generated FROM the schema so they cannot drift
  * validate_page()    a hard gate — invalid records never enter the graph
  * GRAMMAR_NOTE       how to make invalid output structurally impossible

Design rule, inherited from the branching-film work: **the model proposes, the
validator decides.** Nothing reaches Neo4j or the vector store without passing
validate_page(). A record that fails is repaired or quarantined, never coerced.
"""
import json
import re

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
ENTITY_TYPES = ["PERSON", "ORGANIZATION", "PLACE", "EVENT", "PROGRAM", "DATE"]
VISUAL_TYPES = ["photograph", "map", "diagram", "chart", "signature",
                "stamp", "seal", "redaction", "handwriting", "letterhead"]

PAGE_SCHEMA = {
    "type": "object",
    "required": ["transcription", "entities", "visual_elements", "legibility"],
    "additionalProperties": False,
    "properties": {
        "transcription": {
            "type": "string",
            "description": "Verbatim text of the page. Empty string if illegible.",
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "name"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": ENTITY_TYPES},
                    "name": {"type": "string", "minLength": 1},
                    "mention": {
                        "type": "string",
                        "description": "Surface form as written, if different from name.",
                    },
                },
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["subject", "predicate", "object"],
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string", "minLength": 1},
                    "predicate": {"type": "string", "minLength": 1},
                    "object": {"type": "string", "minLength": 1},
                    "date": {"type": "string"},
                },
            },
        },
        "visual_elements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": VISUAL_TYPES},
                    "text": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "legibility": {
            "type": "number", "minimum": 0.0, "maximum": 1.0,
            "description": "0 = unreadable, 1 = crisp. The model's own confidence.",
        },
        "classification_markings": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

# --------------------------------------------------------------------------
# Prompt — generated from the schema so the two cannot drift apart
# --------------------------------------------------------------------------
EXTRACTION_PROMPT = f"""You are transcribing a page from a declassified US government
archive (the CIA CREST collection) for a public historical research database.
These documents are DECLASSIFIED and PUBLIC. Transcribe exactly what is on the
page, including material describing historical covert operations. Do not
editorialise, summarise, or decline — this is archival preservation work.

Return ONLY a JSON object. No prose before or after. No markdown fences.

{json.dumps(PAGE_SCHEMA, indent=2)}

Rules:
- transcription: verbatim text, preserving line order. If a word is unreadable,
  write [illegible] rather than guessing.
- entities.type must be one of: {", ".join(ENTITY_TYPES)}
- visual_elements.type must be one of: {", ".join(VISUAL_TYPES)}
  Record EVERY non-text element: stamps, seals, redaction bars, handwritten
  annotations, photographs, maps, diagrams, letterhead. This is why we use
  vision rather than OCR — do not skip them.
- legibility: your honest confidence this page was read correctly. A low score
  is useful; a confident wrong transcription is harmful.
- NEVER invent text that is not visible. An empty transcription with
  legibility 0.0 is a correct answer for a blank or unreadable page.
"""

# --------------------------------------------------------------------------
# Structured-output enforcement
# --------------------------------------------------------------------------
GRAMMAR_NOTE = """
Prompting for JSON is a request. Constrained decoding is a guarantee.

vLLM (self-hosted, recommended):
    from schema import PAGE_SCHEMA
    payload = {
        "model": MODEL,
        "messages": [...],
        "guided_json": PAGE_SCHEMA,        # vLLM >=0.4, xgrammar/outlines backend
        "temperature": 0,
    }

OpenAI-compatible hosted:
    "response_format": {"type": "json_schema",
                        "json_schema": {"name": "page", "schema": PAGE_SCHEMA,
                                        "strict": True}}

With guided decoding the model CANNOT emit a token that breaks the schema, so
the parse step becomes infallible and the repair loop only handles semantic
errors (hallucinated entities, wrong enum choice) rather than syntax.

Always keep validate_page() active anyway. Guided decoding guarantees shape,
not truth.
"""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_page(obj):
    """Return (ok, errors). Pure stdlib so it runs anywhere, including on a
    GPU box with no extra deps. Mirrors PAGE_SCHEMA."""
    errs = []
    if not isinstance(obj, dict):
        return False, ["not a JSON object"]

    for f in ("transcription", "entities", "visual_elements", "legibility"):
        if f not in obj:
            errs.append(f"missing required field: {f}")

    if "transcription" in obj and not isinstance(obj["transcription"], str):
        errs.append("transcription must be a string")

    lg = obj.get("legibility")
    if lg is not None and (not isinstance(lg, (int, float)) or not 0.0 <= lg <= 1.0):
        errs.append("legibility must be a number in [0,1]")

    ents = obj.get("entities")
    if ents is not None:
        if not isinstance(ents, list):
            errs.append("entities must be an array")
        else:
            for i, e in enumerate(ents):
                if not isinstance(e, dict):
                    errs.append(f"entities[{i}] not an object"); continue
                if e.get("type") not in ENTITY_TYPES:
                    errs.append(f"entities[{i}].type invalid: {e.get('type')!r}")
                if not isinstance(e.get("name"), str) or not e.get("name", "").strip():
                    errs.append(f"entities[{i}].name missing or empty")

    vis = obj.get("visual_elements")
    if vis is not None:
        if not isinstance(vis, list):
            errs.append("visual_elements must be an array")
        else:
            for i, v in enumerate(vis):
                if not isinstance(v, dict):
                    errs.append(f"visual_elements[{i}] not an object"); continue
                if v.get("type") not in VISUAL_TYPES:
                    errs.append(f"visual_elements[{i}].type invalid: {v.get('type')!r}")

    rels = obj.get("relations")
    if rels is not None:
        if not isinstance(rels, list):
            errs.append("relations must be an array")
        else:
            for i, r in enumerate(rels):
                if not isinstance(r, dict):
                    errs.append(f"relations[{i}] not an object"); continue
                for k in ("subject", "predicate", "object"):
                    if not isinstance(r.get(k), str) or not r.get(k, "").strip():
                        errs.append(f"relations[{i}].{k} missing or empty")

    return (len(errs) == 0), errs


def check_grounding(obj):
    """Semantic gate: every entity name should appear in the transcription.

    This is the cheap, deterministic hallucination check. An entity the model
    'knows' but that is not on the page is exactly the failure mode that makes
    a citation-backed archive untrustworthy.

    Returns (ok, ungrounded_names). Advisory — an OCR-mangled name may be
    genuinely present but spelled differently, so treat this as a flag for
    review rather than an automatic reject.
    """
    text = (obj.get("transcription") or "").lower()
    if not text:
        return True, []
    ungrounded = []
    for e in obj.get("entities") or []:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        parts = [p for p in re.split(r"\s+", name.lower()) if len(p) > 2]
        if parts and not any(p in text for p in parts):
            ungrounded.append(name)
    return (len(ungrounded) == 0), ungrounded


def extract_json(text):
    """Recover a JSON object from model output that may be fenced or prose-wrapped."""
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


if __name__ == "__main__":
    good = {"transcription": "Memo from Allen Dulles regarding Project ARTICHOKE.",
            "entities": [{"type": "PERSON", "name": "Allen Dulles"},
                         {"type": "PROGRAM", "name": "ARTICHOKE"}],
            "visual_elements": [{"type": "stamp", "text": "SECRET"}],
            "legibility": 0.9}
    bad = {"transcription": "x", "entities": [{"type": "ALIEN", "name": ""}],
           "visual_elements": [], "legibility": 5}
    halluc = {"transcription": "Routine supply requisition.",
              "entities": [{"type": "PERSON", "name": "Fidel Castro"}],
              "visual_elements": [], "legibility": 0.8}

    for label, rec in (("good", good), ("bad", bad), ("hallucinated", halluc)):
        ok, errs = validate_page(rec)
        g_ok, ung = check_grounding(rec)
        print(f"{label:<14} schema={'PASS' if ok else 'FAIL'}  grounded={'PASS' if g_ok else 'FAIL'}")
        for e in errs:
            print(f"                 - {e}")
        if ung:
            print(f"                 - ungrounded entities: {ung}")
