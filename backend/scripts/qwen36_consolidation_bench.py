#!/usr/bin/env python3
"""Qwen 3.6 consolidation benchmark for UC-Meeting-Ops.

Compares the current default (gemma-4-26b-moe via LiteLLM) against the
proposed consolidation target (Qwen3.6-35B-A3B-Vision via direct midboy1
endpoint) on the four workloads that matter for Meeting-Ops:

  A. Final summarization (auto_summarization_service.py-style call)
  B. Slice summary (summary_slices.py-style call)
  C. Tool use (meeting_rag.py-style call with the 8 MCP tools schema)
  D. Latency: time-to-first-token + total elapsed across 5 small prompts

Run from inside meet-backend so the env vars / hostnames resolve the way
the production stack sees them.

Usage:
    docker exec -i meet-backend python /tmp/qwen36_consolidation_bench.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

import httpx

# Hardcode the two endpoints rather than re-derive from env so the bench
# is reproducible across runs.
GEMMA = {
    "label": "gemma-4-26b-moe (LiteLLM)",
    "endpoint": "http://unicorn-litellm:4000/v1",
    "model": "gemma-4-26b-moe",
    "api_key": os.getenv("LITELLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
    "kind": "litellm",
}
QWEN = {
    "label": "Qwen3.6-35B-A3B-Vision (midboy1 direct)",
    "endpoint": "http://llm-gateway:8088/v1",
    "model": "Qwen3.6-35B-A3B-Vision",
    "api_key": "",
    "kind": "qwen36",
}


TRANSCRIPT_103 = """
Aaron: All right, the question on hardware resale — David's got the lot
of laptops in. Are we going to keep doing this through eBay or pivot to
StockX-style auction batches? StockX gives us better price discovery on
the high-end GPUs but the fee is brutal on the low-end laptops.

David: I think we should split it. Anything under $400 cost basis goes
on eBay with Best Offer disabled by default. Anything above that, we
list on StockX. The fee math works out because the price spreads on the
higher-end stuff are bigger.

Aaron: OK, action item for me: I'll update the listing tool to support
the dual-marketplace routing based on a cost threshold. We need that by
end of next week because Shafen wants to ship his Mac Studio.

David: I can pull the pricing comp data from the last 90 days so we can
calibrate the threshold. I'll have that to you Thursday.

Aaron: Perfect. Other topic — the hydration jello venture. I talked to
the food scientist and she said the agar-based formulation is doable but
we need to commit to a $40k tooling spend up front for the bottle dies.
That's a big check.

David: What's the projected sell-through? If we can move 50k units in
the first quarter at $4 each that's $200k revenue against the $40k
tooling. Sounds fine.

Aaron: Yeah but the projected sell-through is based on a marketing plan
we haven't built yet. I want to talk to the team about whether we're
ready to commit before I write that check.

David: Fair. Action item for you to schedule the marketing review by
end of month.

Aaron: Got it. Decision: hold the tooling commitment until after we
have a real marketing plan. We'll revisit at the November all-hands.

David: Logged. One more thing — Postmark added a new domain verification
requirement and we got bounced on two outbound campaigns yesterday. I
need to know if you want me to fix it or if you're handling it.

Aaron: I'll handle it. Action item for me, today. Anything else?

David: No, that's it.

Aaron: OK, ending the recording.
""".strip()


SLICE_PRIOR_SUMMARY = """
Aaron and David are discussing hardware resale strategy. David proposed
splitting listings between eBay (under $400) and StockX (above). Aaron
took an action item to update the listing tool with cost-threshold
routing by end of next week.
""".strip()

SLICE_NEW_TRANSCRIPT = """
Aaron: OK, other topic — the hydration jello venture. I talked to the
food scientist and she said the agar-based formulation is doable but we
need to commit to a $40k tooling spend up front for the bottle dies.

David: What's the projected sell-through? If we can move 50k units in
the first quarter at $4 each that's $200k revenue against the $40k
tooling. Sounds fine.

Aaron: Yeah but the projected sell-through is based on a marketing plan
we haven't built yet. I want to talk to the team about whether we're
ready to commit before I write that check.

Aaron: Decision: hold the tooling commitment until after we have a real
marketing plan. We'll revisit at the November all-hands.
""".strip()


MEETING_RAG_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_meetings",
            "description": "Search across the user's meetings by topic, speaker, keyword, or any phrase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_about_meetings",
            "description": "Answer a question by retrieving top matching meetings and synthesizing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_meetings",
            "description": "List the user's most recent meetings, newest first. Optionally filter by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "status": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_details",
            "description": "Fetch full metadata + structured summary for one meeting.",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_transcript",
            "description": "Fetch the transcript text for one meeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 10000},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_with_meeting",
            "description": "Ask a focused question about ONE specific meeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["session_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analytics",
            "description": "Return aggregate meeting statistics over a time range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_range": {"type": "string", "enum": ["week", "month", "quarter", "year"], "default": "month"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_insights",
            "description": "Return cached AI insights (keywords, sentiment, engagement) for one meeting.",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        },
    },
]


def _headers(model_cfg: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if model_cfg.get("api_key"):
        h["Authorization"] = f"Bearer {model_cfg['api_key']}"
    return h


def _call(model_cfg: dict, payload: dict, timeout: float = 480.0) -> tuple[Optional[dict], float, float]:
    """POST chat/completions, return (data, ttfb_seconds, total_seconds).

    For non-streamed calls ttfb == total. We use this signature for parity.
    """
    payload = dict(payload)
    payload.setdefault("model", model_cfg["model"])
    payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{model_cfg['endpoint']}/chat/completions",
                headers=_headers(model_cfg),
                json=payload,
            )
            elapsed = time.time() - t0
            if resp.status_code != 200:
                print(f"  ERROR {resp.status_code}: {resp.text[:300]}", flush=True)
                return None, elapsed, elapsed
            return resp.json(), elapsed, elapsed
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"  EXCEPTION: {exc}", flush=True)
        return None, elapsed, elapsed


def _stream_ttfb(model_cfg: dict, payload: dict, timeout: float = 480.0) -> tuple[Optional[str], float, float, int]:
    """Stream chat/completions, return (full_text, ttfb_seconds, total_seconds, output_tokens_est)."""
    payload = dict(payload)
    payload.setdefault("model", model_cfg["model"])
    payload.setdefault("stream", True)
    payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
    t0 = time.time()
    ttfb = None
    chunks: list[str] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{model_cfg['endpoint']}/chat/completions",
                                headers=_headers(model_cfg), json=payload) as resp:
                if resp.status_code != 200:
                    total = time.time() - t0
                    print(f"  STREAM ERROR {resp.status_code}", flush=True)
                    return None, total, total, 0
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    blob = line[len("data:"):].strip()
                    if blob == "[DONE]":
                        break
                    try:
                        ev = json.loads(blob)
                    except Exception:
                        continue
                    choices = ev.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    tok = delta.get("content") or ""
                    if tok and ttfb is None:
                        ttfb = time.time() - t0
                    if tok:
                        chunks.append(tok)
        total = time.time() - t0
        out = "".join(chunks)
        toks = max(1, len(out.split()))
        return out, ttfb if ttfb is not None else total, total, toks
    except Exception as exc:
        total = time.time() - t0
        print(f"  STREAM EXCEPTION: {exc}", flush=True)
        return None, total, total, 0


# ---------------------------------------------------------------------------
# Test A — Final summarization
# ---------------------------------------------------------------------------
def test_a_final_summary(model_cfg: dict) -> dict:
    system_prompt = (
        "You are a meeting assistant. Provide clear, structured meeting summaries."
    )
    user_prompt = (
        "Create a meeting summary with:\n"
        "1. Main Topics (3-5 bullet points)\n"
        "2. Key Decisions Made\n"
        "3. Action Items (who, what, when)\n"
        "4. Follow-up Required\n"
        "Keep it clear and organized.\n\n"
        f"Transcript:\n{TRANSCRIPT_103}"
    )
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 800,
        "temperature": 0.3,
    }
    data, _, total = _call(model_cfg, payload)
    if not data:
        return {"ok": False, "total_s": total, "text": "", "tok_per_s": 0}
    msg = data["choices"][0].get("message", {})
    text = (msg.get("content") or "").strip()
    rc = (msg.get("reasoning_content") or "").strip()
    usage = data.get("usage", {})
    out_toks = usage.get("completion_tokens", 0)
    tps = out_toks / total if total > 0 else 0
    text_for_quality = text if text else rc

    # Quality probes
    txt_lower = text_for_quality.lower()
    decision_hit = any(p in txt_lower for p in ["decision", "hold the tooling", "until after"])
    action_aaron = "aaron" in txt_lower and ("listing" in txt_lower or "postmark" in txt_lower or "marketing review" in txt_lower)
    action_david = "david" in txt_lower and ("pricing comp" in txt_lower or "thursday" in txt_lower)

    # Thinking-mode bleed
    bleed_through = bool(rc) or text_for_quality.lstrip().startswith("<think")

    return {
        "ok": True,
        "total_s": total,
        "out_tokens": out_toks,
        "tok_per_s": tps,
        "text": text_for_quality,
        "reasoning_content": rc,
        "decision_extracted": decision_hit,
        "action_aaron_hit": action_aaron,
        "action_david_hit": action_david,
        "thinking_bleed": bleed_through,
    }


# ---------------------------------------------------------------------------
# Test B — Slice summary
# ---------------------------------------------------------------------------
SLICE_SYSTEM_PROMPT = (
    "You are a live meeting summarizer. The user is currently in a meeting and "
    "wants a running summary that updates as new conversation happens. Given the "
    "transcript so far and the previous summary, produce an updated summary that:\n"
    "- Leads with what is NEW since the previous summary\n"
    "- Preserves key context, decisions, and action items from before\n"
    "- Stays under 200 words\n"
    "- Reads naturally, like notes a thoughtful attendee would take\n\n"
    "Do not list speakers verbatim. Do not repeat the previous summary word-for-word."
)


def test_b_slice(model_cfg: dict) -> dict:
    user_prompt = (
        "Previous running summary:\n"
        f"{SLICE_PRIOR_SUMMARY}\n\n"
        "New transcript since that summary (most recent at the bottom):\n"
        f"{SLICE_NEW_TRANSCRIPT}"
    )
    payload = {
        "messages": [
            {"role": "system", "content": SLICE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 600,
        "temperature": 0.3,
    }
    out, ttfb, total, est_toks = _stream_ttfb(model_cfg, payload)
    if out is None:
        return {"ok": False, "ttfb_s": ttfb, "total_s": total, "text": ""}
    txt_lower = out.lower()
    leads_with_new = ("jello" in txt_lower or "tooling" in txt_lower or "hydration" in txt_lower) and out.split("\n")[0].lower().startswith(("new", "- ", "* ", "what's new", "what is new", "*  new", "  - "))
    preserved_prior = "ebay" in txt_lower or "stockx" in txt_lower or "listing tool" in txt_lower or "threshold" in txt_lower
    bleeds_through = out.lstrip().startswith("<think") or "</think>" in out[:400]
    word_count = len(out.split())
    return {
        "ok": True,
        "ttfb_s": ttfb,
        "total_s": total,
        "word_count": word_count,
        "leads_with_new": leads_with_new,
        "preserved_prior": preserved_prior,
        "under_200_words": word_count <= 230,  # tolerance
        "thinking_bleed": bleeds_through,
        "text": out,
    }


# ---------------------------------------------------------------------------
# Test C — Tool use
# ---------------------------------------------------------------------------
def test_c_tools(model_cfg: dict) -> dict:
    system_prompt = (
        "You are the Meeting Assistant for UC-Meeting-Ops. Help the user "
        "understand their recorded meetings. Always ground answers in tool results. "
        "Standard workflow: 1) call search_meetings, 2) if one obvious match, call "
        "chat_with_meeting on that session_id."
    )
    user_message = "What action items did Aaron commit to in his last 3 meetings?"
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "tools": MEETING_RAG_TOOLS,
        "tool_choice": "auto",
        "max_tokens": 1024,
        "temperature": 0.3,
    }
    data, _, total = _call(model_cfg, payload)
    if not data:
        return {"ok": False, "total_s": total}
    choice = data["choices"][0]
    msg = choice.get("message", {})
    finish = choice.get("finish_reason")
    tool_calls = msg.get("tool_calls") or []
    bleed = bool((msg.get("reasoning_content") or "").strip()) or (msg.get("content") or "").lstrip().startswith("<think")

    # Validate first tool call shape
    valid_call = False
    chosen_tool = None
    parsed_args = None
    if tool_calls:
        tc = tool_calls[0]
        fn = tc.get("function") or {}
        chosen_tool = fn.get("name")
        raw_args = fn.get("arguments")
        try:
            if isinstance(raw_args, str):
                parsed_args = json.loads(raw_args) if raw_args.strip() else {}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = None
            valid_call = chosen_tool in {t["function"]["name"] for t in MEETING_RAG_TOOLS} and isinstance(parsed_args, dict)
        except Exception:
            valid_call = False

    sensible = chosen_tool in ("list_meetings", "search_meetings", "ask_about_meetings", "get_analytics")
    return {
        "ok": True,
        "total_s": total,
        "finish_reason": finish,
        "emitted_tool_calls": bool(tool_calls),
        "n_tool_calls": len(tool_calls),
        "first_tool": chosen_tool,
        "first_args": parsed_args,
        "valid_call_shape": valid_call,
        "sensible_choice": sensible,
        "thinking_bleed": bleed,
        "raw_content_preview": (msg.get("content") or "")[:200],
    }


# ---------------------------------------------------------------------------
# Test D — Latency
# ---------------------------------------------------------------------------
LATENCY_PROMPTS = [
    "What is 2 plus 2?",
    "Write a single-sentence definition of OAuth.",
    "Name one decision a meeting summarizer might miss.",
    "List 3 reasons to record meetings.",
    "What's a good follow-up question after 'we decided to ship Friday'?",
]

def test_d_latency(model_cfg: dict) -> dict:
    rows = []
    for prompt in LATENCY_PROMPTS:
        payload = {
            "messages": [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 80,
            "temperature": 0.3,
        }
        out, ttfb, total, est_toks = _stream_ttfb(model_cfg, payload, timeout=120)
        rows.append({
            "prompt": prompt,
            "ttfb_s": ttfb,
            "total_s": total,
            "est_out_tokens": est_toks,
            "ok": out is not None,
        })
    ok_rows = [r for r in rows if r["ok"]]
    if not ok_rows:
        return {"ok": False, "rows": rows}
    avg_ttfb = sum(r["ttfb_s"] for r in ok_rows) / len(ok_rows)
    avg_total = sum(r["total_s"] for r in ok_rows) / len(ok_rows)
    total_toks = sum(r["est_out_tokens"] for r in ok_rows)
    total_time = sum(r["total_s"] for r in ok_rows)
    avg_tps = total_toks / total_time if total_time > 0 else 0
    return {
        "ok": True,
        "rows": rows,
        "avg_ttfb_s": avg_ttfb,
        "avg_total_s": avg_total,
        "avg_tok_per_s": avg_tps,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run(model_cfg: dict) -> dict:
    print(f"\n{'='*72}")
    print(f"  {model_cfg['label']}")
    print(f"  endpoint={model_cfg['endpoint']} model={model_cfg['model']}")
    print(f"{'='*72}\n")

    out = {"label": model_cfg["label"], "endpoint": model_cfg["endpoint"], "model": model_cfg["model"]}

    print("Test A — Final summarization...")
    a = test_a_final_summary(model_cfg)
    print(f"  ok={a['ok']} total={a.get('total_s'):.2f}s tok/s={a.get('tok_per_s', 0):.1f}")
    print(f"  decision_extracted={a.get('decision_extracted')} action_aaron_hit={a.get('action_aaron_hit')} action_david_hit={a.get('action_david_hit')} bleed={a.get('thinking_bleed')}")
    out["test_a"] = a

    print("\nTest B — Slice summary (streamed)...")
    b = test_b_slice(model_cfg)
    print(f"  ok={b['ok']} ttfb={b.get('ttfb_s'):.2f}s total={b.get('total_s'):.2f}s words={b.get('word_count')}")
    print(f"  leads_new={b.get('leads_with_new')} preserved_prior={b.get('preserved_prior')} under_200w={b.get('under_200_words')} bleed={b.get('thinking_bleed')}")
    out["test_b"] = b

    print("\nTest C — Tool use...")
    c = test_c_tools(model_cfg)
    print(f"  ok={c['ok']} total={c.get('total_s'):.2f}s finish={c.get('finish_reason')}")
    print(f"  n_tool_calls={c.get('n_tool_calls')} first_tool={c.get('first_tool')} valid_shape={c.get('valid_call_shape')} sensible={c.get('sensible_choice')} bleed={c.get('thinking_bleed')}")
    print(f"  first_args={c.get('first_args')}")
    out["test_c"] = c

    print("\nTest D — Latency (5 small prompts)...")
    d = test_d_latency(model_cfg)
    print(f"  avg_ttfb={d.get('avg_ttfb_s', 0):.2f}s avg_total={d.get('avg_total_s', 0):.2f}s avg_tok/s={d.get('avg_tok_per_s', 0):.1f}")
    out["test_d"] = d

    return out


def main():
    results = []
    for cfg in [GEMMA, QWEN]:
        results.append(run(cfg))

    print(f"\n\n{'#'*72}")
    print("# SUMMARY")
    print(f"{'#'*72}\n")
    for r in results:
        print(f"\n=== {r['label']} ===")
        ta = r["test_a"]
        tb = r["test_b"]
        tc = r["test_c"]
        td = r["test_d"]
        print(f"A. final summary    : {ta.get('total_s', 0):.1f}s  tok/s={ta.get('tok_per_s', 0):.1f}  decision={ta.get('decision_extracted')} aaron={ta.get('action_aaron_hit')} david={ta.get('action_david_hit')} bleed={ta.get('thinking_bleed')}")
        print(f"B. slice summary    : ttfb={tb.get('ttfb_s', 0):.2f}s  total={tb.get('total_s', 0):.1f}s  words={tb.get('word_count', 0)}  bleed={tb.get('thinking_bleed')}")
        print(f"C. tool use         : total={tc.get('total_s', 0):.2f}s  tool={tc.get('first_tool')}  valid={tc.get('valid_call_shape')} sensible={tc.get('sensible_choice')} bleed={tc.get('thinking_bleed')}")
        print(f"D. latency (avg)    : ttfb={td.get('avg_ttfb_s', 0):.2f}s  total={td.get('avg_total_s', 0):.2f}s  tok/s={td.get('avg_tok_per_s', 0):.1f}")

    out_path = "/tmp/qwen36_bench_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
