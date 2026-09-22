#!/usr/bin/env python3
"""
extract.py — optional second reading of a NEW order, by a small model.

WHY
  51 governors write orders however they like. The regex parser handles the
  phrasings we have seen; the long tail it cannot scope now reports UNKNOWN,
  which is honest but blank. This asks Claude Haiku to read the same order and
  say what it states - scope, dates, authority, who is honoured - so an order
  we would otherwise withhold can be resolved.

THE RULES THIS KEEPS
  - It EXTRACTS, it never decides. Nothing here says half-staff or full staff.
    Our own rules read these fields and decide, exactly as before.
  - The regex parser still runs on every order. Where both make a definite
    claim and the claims differ, the answer is UNKNOWN - two methods
    disagreeing is evidence of doubt, not a menu to choose from.
  - Every field must be backed by a verbatim quote from the order. If the
    quote is not in the text, the field is dropped. A model that invents a
    sentence cannot put a state at half-staff.
  - It runs once per order, cached by URL with the rest of that order's facts,
    so a re-run costs nothing.
  - If it fails, is slow, or has no API key, the run continues on the regex
    parser alone. It can never block or fail a run.

SETUP
  Optional. Set ANTHROPIC_API_KEY (a GitHub secret in CI). Without it this
  module does nothing and says so once.
"""

import json
import os
import re

MODEL = "claude-haiku-4-5"
MAX_BODY_CHARS = 12_000
TIMEOUT_S = 20
TOOL_NAME = "record_order"

SYSTEM = """You read one US state flag-lowering order and record only what it \
states. You are an extractor, not a judge.

Rules:
- Record only what the text says. Never infer, complete, or use outside \
knowledge. If the order does not state something, say so ("unknown" or null).
- Every field you fill must be supported by a quote copied VERBATIM from the \
order, character for character. Never paraphrase a quote. If you cannot quote \
it, the field is unknown.
- scope is "statewide" only if the order says the flags come down across the \
whole state (for example "statewide", "throughout the state", "all state \
buildings", "all public buildings and grounds", "flags in <State>"). It is \
"limited" if the order covers only part of the state - one city, one county, \
the Capitol, a single building, or authority delegated to a local official. \
If the order does not say how far it reaches, scope is "unknown". Most orders \
that merely honour a local person are still statewide; go by what is written \
about the flags, not by who is honoured.
- Dates are ISO (YYYY-MM-DD). Use only dates the order states. A date with no \
year takes the year from the order's own date if that is stated, otherwise \
leave it null.
- Never say whether flags are currently at half-staff. That is not your job."""

TOOL = {
    "name": TOOL_NAME,
    "description": "Record the fields an order states about lowering flags.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scope": {"type": "string", "enum": ["statewide", "limited", "unknown"],
                      "description": "How far the order reaches, as stated."},
            "scope_quote": {"type": "string",
                            "description": "Verbatim sentence stating the reach, "
                                           "or empty string if none."},
            "start_date": {"type": ["string", "null"], "description": "ISO date or null."},
            "end_date": {"type": ["string", "null"], "description": "ISO date or null."},
            "date_quote": {"type": "string",
                           "description": "Verbatim text stating the dates, or empty."},
            "authority": {"type": "string",
                          "enum": ["governor", "president", "other", "unknown"]},
            "honoree": {"type": ["string", "null"],
                        "description": "Who or what the order honours."},
        },
        "required": ["scope", "scope_quote", "start_date", "end_date", "date_quote",
                     "authority", "honoree"],
    },
}


def enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and \
        os.environ.get("FLAGSTAFF_EXTRACT", "1") != "0"


def _squash(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def quoted(quote, body):
    """Is this quote really in the order? Whitespace-insensitive, otherwise
    exact. This is the anti-hallucination gate: a field whose quote is not in
    the source is discarded, so an invented sentence cannot lower a flag."""
    q = _squash(quote)
    return bool(q) and len(q) >= 12 and q in _squash(body)


def call_model(body, state_name):
    """One Haiku call. Returns the tool input dict, or None. Seam for tests."""
    import anthropic

    client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)
    prompt = (f"State: {state_name or 'unknown'}\n\n"
              f"Order text:\n\"\"\"\n{body[:MAX_BODY_CHARS]}\n\"\"\"")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            out = dict(block.input)
            u = resp.usage
            out["_usage"] = {"in": u.input_tokens, "out": u.output_tokens}
            return out
    return None


def extract(body, state_name):
    """Extracted fields for one order, or None. Never raises."""
    if not enabled() or not body:
        return None
    try:
        raw = call_model(body, state_name)
    except Exception as e:                      # never block a run
        print(f"    extract: {type(e).__name__}: {str(e)[:120]} - regex only")
        return None
    if not raw:
        return None
    out = {"authority": raw.get("authority"), "honoree": raw.get("honoree"),
           "usage": raw.get("_usage")}
    # Each field survives only if its quote is really in the order.
    if quoted(raw.get("scope_quote"), body):
        out["scope"] = raw.get("scope")
        out["scope_quote"] = (raw.get("scope_quote") or "")[:200]
    else:
        out["scope"] = "unknown"
        out["scope_dropped"] = bool(raw.get("scope_quote"))
    if quoted(raw.get("date_quote"), body):
        out["start_date"], out["end_date"] = raw.get("start_date"), raw.get("end_date")
        out["date_quote"] = (raw.get("date_quote") or "")[:200]
    else:
        out["start_date"] = out["end_date"] = None
        out["dates_dropped"] = bool(raw.get("date_quote"))
    return out


def merge(facts, llm):
    """Fold an extraction into the regex facts.

    Agreement strengthens nothing (the answer was already there). A definite
    disagreement on scope or dates makes the field unknown. Where one method
    has nothing and the other is definite, the definite one is used and
    recorded as such - that is the long tail this exists for.
    """
    if not llm:
        return facts
    f = dict(facts)
    f["llm"] = {k: v for k, v in llm.items() if k != "usage"}
    f["llm_usage"] = llm.get("usage")

    r_scope, m_scope = f.get("scope", "unknown"), llm.get("scope", "unknown")
    if r_scope != "unknown" and m_scope != "unknown" and r_scope != m_scope:
        f["scope"] = "unknown"
        f["scope_evidence"] = (f"regex says {r_scope}, the model says {m_scope} "
                               f"- two readings disagree, so scope is unknown")
    elif r_scope == "unknown" and m_scope != "unknown":
        f["scope"] = m_scope
        f["scope_evidence"] = f"from the order text (model): {llm.get('scope_quote', '')[:90]}"

    r_dates = (f.get("body_start"), f.get("body_end"))
    m_dates = (llm.get("start_date"), llm.get("end_date"))
    both = all(x for x in r_dates) and all(x for x in m_dates)
    if both and r_dates != m_dates:
        f["body_start"] = f["body_end"] = None
        f["dates_disputed"] = f"regex {r_dates}, model {m_dates}"
    elif not any(r_dates) and all(m_dates):
        f["body_start"], f["body_end"] = m_dates
        f["dates_from_model"] = llm.get("date_quote", "")[:90]
    return f
