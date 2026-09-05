"""
agent_explain.py
-----------------
A deliberately small "AI layer" on top of matches.csv / exceptions.csv.

What it does:
    - summarize_exceptions() : portfolio-level summary of what needs attention
    - explain_case(case_id)  : explain one case using ONLY its stored evidence
    - answer_question(q)     : grounded Q&A, abstains when data doesn't cover it

How grounding works:
    Every call first builds a small JSON "context" block by looking the
    question up in matches.csv / exceptions.csv (by case_id, by Razorpay-style
    entity id, or by a rupee amount mentioned in the question). That context -
    and NOTHING else - is what gets shown to the model, with an explicit
    instruction to say so if the context doesn't answer the question.

    If no ANTHROPIC_API_KEY is configured, everything still works: a
    deterministic, template-based responder reads the exact same context
    and produces a plain-English answer. This means the demo (and the
    "abstain instead of hallucinate" behaviour) never depends on having an
    API key wired up.

Run:
    python agent_explain.py                     # interactive mode
    python agent_explain.py "Why is EXC-004 unresolved?"
"""

import json
import os
import re
import sys

import pandas as pd

import config

CASE_ID_RE = re.compile(r"\b(EXC|MATCH)-\d+\b", re.IGNORECASE)
ENTITY_ID_RE = re.compile(r"\b(order|pay|setl|rfnd)_[A-Za-z0-9]+\b")
AMOUNT_RE = re.compile(r"(?:₹|rs\.?\s?)\s?([\d][\d,]*(?:\.\d+)?)", re.IGNORECASE)
METHOD_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in config.PAYMENT_METHODS) + r")\b", re.IGNORECASE
)

SYSTEM_PROMPT = """You are a finance-ops assistant inside an AI Finance Controller that
reconciles Razorpay-style payments, settlements, and refunds.

Rules you must always follow:
1. Answer ONLY using the JSON evidence given to you in the user message. Never use
   outside knowledge about Razorpay, banks, or this merchant.
2. Never state a cause as fact unless it is explicitly present in the evidence.
   If a bank record is simply missing, say it is missing - do not claim why.
3. If the evidence does not contain enough information to answer the question,
   say plainly that the available reconciliation data does not establish the
   answer, and name what data would be needed instead.
4. Be concise: a short paragraph or a few bullet points, written for a finance
   operator, not a chat transcript.
"""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_tables():
    matches = pd.read_csv(config.MATCHES_CSV)
    exceptions = pd.read_csv(config.EXCEPTIONS_CSV)
    return matches, exceptions


def load_metrics():
    if os.path.exists(config.METRICS_JSON):
        with open(config.METRICS_JSON) as f:
            return json.load(f)
    return None


# ---------------------------------------------------------------------------
# Context building - this IS the grounding. If it's not in this dict, the
# model (or the offline fallback) is told not to answer from thin air.
# ---------------------------------------------------------------------------

def find_case_by_id(case_id, matches, exceptions):
    case_id = case_id.upper()
    for df in (exceptions, matches):
        hit = df[df["case_id"].astype(str).str.upper() == case_id]
        if len(hit):
            return hit.iloc[0].to_dict()
    return None


def find_case_by_entity_id(entity_id, matches, exceptions):
    for df in (exceptions, matches):
        cols = [c for c in ["entity_id", "order_id", "payment_id", "settlement_id", "refund_id"] if c in df.columns]
        mask = False
        for c in cols:
            mask = mask | (df[c].astype(str) == entity_id)
        hit = df[mask]
        if len(hit):
            return hit.iloc[0].to_dict()
    return None


def find_case_by_amount(amount_rupees, matches, exceptions, tolerance=1.0):
    combined = pd.concat([exceptions, matches], ignore_index=True)
    for col in ("expected_amount", "actual_amount"):
        if col not in combined.columns:
            continue
        vals = pd.to_numeric(combined[col], errors="coerce")
        hit = combined[(vals - amount_rupees).abs() <= tolerance]
        if len(hit):
            return hit.iloc[0].to_dict()
    return None


def find_cases_by_method(method, matches, exceptions, only_needing_attention=True):
    """Used for questions like 'which EMI transactions failed reconciliation' -
    returns every matching row, not just one, since this is a portfolio-level
    question, not a single-case lookup."""
    pool = exceptions if only_needing_attention else pd.concat([matches, exceptions], ignore_index=True)
    if "method" not in pool.columns or not len(pool):
        return []
    hits = pool[pool["method"].astype(str).str.upper() == method.upper()]
    return hits.to_dict("records")


def build_aggregate_context(matches, exceptions):
    metrics = load_metrics()
    return {
        "type": "aggregate",
        "metrics": metrics,
        "exceptions_by_case_type": exceptions["case_type"].value_counts().to_dict() if len(exceptions) else {},
        "exceptions_by_decision": exceptions["decision"].value_counts().to_dict() if len(exceptions) else {},
        "total_matches": len(matches),
        "total_exceptions": len(exceptions),
    }


def build_context(question, matches, exceptions):
    """Look the question up in the data. Returns a dict with a "type" key:
    "case" (found), "not_found" (a specific reference was given but it
    doesn't exist in the data), or "aggregate" (no specific reference -
    portfolio-level context only)."""
    m = CASE_ID_RE.search(question)
    if m:
        case_id = m.group(0)
        row = find_case_by_id(case_id, matches, exceptions)
        if row:
            return {"type": "case", "query": case_id, "case": row}
        return {"type": "not_found", "query": case_id}

    m = ENTITY_ID_RE.search(question)
    if m:
        entity_id = m.group(0)
        row = find_case_by_entity_id(entity_id, matches, exceptions)
        if row:
            return {"type": "case", "query": entity_id, "case": row}
        return {"type": "not_found", "query": entity_id}

    m = AMOUNT_RE.search(question)
    if m:
        amount = float(m.group(1).replace(",", ""))
        row = find_case_by_amount(amount, matches, exceptions)
        if row:
            return {"type": "case", "query": f"Rs {amount}", "case": row}
        return {"type": "not_found", "query": f"Rs {amount}"}

    m = METHOD_RE.search(question)
    if m:
        method = m.group(1).upper()
        cases = find_cases_by_method(method, matches, exceptions)
        total_value = round(sum(float(c.get("expected_amount", 0) or 0) for c in cases), 2)
        return {"type": "method_filter", "method": method, "count": len(cases),
                "total_value": total_value, "cases": cases}

    return build_aggregate_context(matches, exceptions)


# ---------------------------------------------------------------------------
# LLM call (optional) - falls back to None if no API key or on any error,
# so the caller can always drop back to the deterministic responder.
# ---------------------------------------------------------------------------

def load_dotenv_if_present():
    """Tiny, dependency-free .env loader - avoids adding python-dotenv just
    for a couple of local vars during a hackathon."""
    env_path = os.path.join(config.BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def call_claude(user_prompt):
    load_dotenv_if_present()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get("FINANCE_AGENT_MODEL", "claude-sonnet-5")
        message = client.messages.create(
            model=model,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text").strip()
    except Exception as exc:  # noqa: BLE001 - any API/SDK issue -> fall back gracefully
        print(f"[agent] Claude API call failed, falling back to offline mode: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Offline (no-API-key) deterministic responder - reads the SAME context.
# ---------------------------------------------------------------------------

def fmt(value):
    """Blank/NaN cells (e.g. no matching bank row was ever found) should
    read as "n/a", not the literal string "nan"."""
    if value is None:
        return "n/a"
    try:
        if pd.isna(value):
            return "n/a"
    except (TypeError, ValueError):
        pass
    return value


def offline_answer(context):
    if context["type"] == "not_found":
        return (f"The available reconciliation data does not contain a case matching "
                f"'{context['query']}'. It may not exist, or may need regenerating.")

    if context["type"] == "case":
        c = context["case"]
        lines = [
            f"{c.get('case_id', c.get('entity_id'))} ({c.get('case_type', 'CLEAN_MATCH')}):",
            f"  Decision: {c.get('decision', 'AUTO_RESOLVE')} | Confidence: {fmt(c.get('confidence_score'))} | "
            f"Candidates considered: {fmt(c.get('candidate_count'))}",
            f"  Expected amount: Rs {fmt(c.get('expected_amount'))} | Actual amount: {fmt(c.get('actual_amount'))} | "
            f"Variance: {fmt(c.get('variance'))}",
        ]
        if c.get("reason"):
            lines.append(f"  Evidence-based reason: {c['reason']}")
        if c.get("recommended_action"):
            lines.append(f"  Recommended next step: {c['recommended_action']}")
        return "\n".join(lines)

    if context["type"] == "method_filter":
        if context["count"] == 0:
            return (f"No {context['method']} transactions currently need attention - "
                    f"the available reconciliation data does not show any open cases for this method.")
        lines = [f"{context['count']} {context['method']} case(s) need attention "
                 f"(Rs {context['total_value']:,.2f} total):"]
        for c in context["cases"]:
            lines.append(
                f"  - {c.get('case_id')} ({c.get('case_type')}): Rs {fmt(c.get('expected_amount'))}, "
                f"{c.get('decision')} - {c.get('reason', '')}"
            )
        return "\n".join(lines)

    # aggregate
    m = context.get("metrics")
    parts = [f"Portfolio summary ({context['total_matches']} auto-resolved, "
             f"{context['total_exceptions']} needing attention):"]
    for case_type, count in context.get("exceptions_by_case_type", {}).items():
        parts.append(f"  - {case_type}: {count} case(s)")
    if m:
        parts.append(f"  Unresolved exposure: Rs {m['unresolved_rupee_value']:,.2f} across {m['unresolved_count']} case(s)")
        parts.append(f"  Human review exposure: Rs {m['human_review_value']:,.2f} across {m['human_review_count']} case(s)")
        parts.append(f"  auto_match_precision={m['auto_match_precision']}, "
                     f"exception_detection_recall={m['exception_detection_recall']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def answer_question(question):
    matches, exceptions = load_tables()
    context = build_context(question, matches, exceptions)

    prompt = (
        f"Finance operator's question: {question}\n\n"
        f"Structured evidence you may use (JSON):\n{json.dumps(context, default=str, indent=2)}"
    )
    reply = call_claude(prompt)
    return reply if reply else offline_answer(context)


def explain_case(case_id):
    matches, exceptions = load_tables()
    row = find_case_by_id(case_id, matches, exceptions) or find_case_by_entity_id(case_id, matches, exceptions)
    if row is None:
        return f"The available reconciliation data does not contain a case matching '{case_id}'."

    context = {"type": "case", "query": case_id, "case": row}
    prompt = (
        f"Explain this reconciliation case to a finance operator: what happened, what evidence "
        f"supports the classification, and what the recommended next step is.\n\n"
        f"Structured evidence (JSON):\n{json.dumps(context, default=str, indent=2)}"
    )
    reply = call_claude(prompt)
    return reply if reply else offline_answer(context)


def summarize_exceptions():
    matches, exceptions = load_tables()
    context = build_aggregate_context(matches, exceptions)
    prompt = (
        "Summarize the current reconciliation exception backlog for a finance operator "
        "in a short paragraph plus a few bullet points, using only this evidence:\n\n"
        f"{json.dumps(context, default=str, indent=2)}"
    )
    reply = call_claude(prompt)
    return reply if reply else offline_answer(context)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    load_dotenv_if_present()
    mode = "online (Claude API)" if os.environ.get("ANTHROPIC_API_KEY") else "offline (no ANTHROPIC_API_KEY set)"
    print(f"[agent_explain] running in {mode} mode")

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(answer_question(question))
        return

    print("AI Finance Controller agent. Ask a question, type 'summary', or 'quit'.")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question.lower() in ("quit", "exit"):
            break
        if question.lower() == "summary":
            print(summarize_exceptions())
        else:
            print(answer_question(question))


if __name__ == "__main__":
    main()
