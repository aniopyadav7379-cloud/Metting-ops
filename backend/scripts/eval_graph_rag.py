#!/usr/bin/env python3
"""A/B eval harness for graph-augmented meeting retrieval.

Runs a fixed list of real cross-meeting queries against the live
``services.agent_tools.search_meetings_impl`` with graph augmentation OFF and
ON, then prints a paste-ready comparison table (top-3 per mode + a
"reorder happened" flag). Read-only and idempotent — it never writes to the
database, Qdrant, or Brigade, so it is safe to run repeatedly.

This is the check Aaron runs to decide whether to flip
``MEETING_RAG_GRAPH_AUGMENTATION`` on. It does NOT change any default.

Usage (inside the backend container / venv):

    python scripts/eval_graph_rag.py                 # auto-pick busiest org
    python scripts/eval_graph_rag.py --org-id 1
    python scripts/eval_graph_rag.py --org-slug magic-unicorn
    python scripts/eval_graph_rag.py --limit 5 --query "Shafen loans" --query "Citadel decisions"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make the backend root importable whether invoked as `python scripts/eval_graph_rag.py`
# (sys.path[0] == scripts/) or `python -m scripts.eval_graph_rag`.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# The four anchor queries from the v3.11.0 verify report.
ANCHOR_QUERIES = [
    "Shafen loans",
    "Citadel decisions",
    "Rocky action items",
    "automatic summarization manual button transcription",
]

TITLE_WIDTH = 52


def _truncate(text: str, width: int = TITLE_WIDTH) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _resolve_org_id(db, org_id, org_slug):
    from sqlalchemy import func

    from auth.models import Organization
    from database.models import RecordingSession

    if org_id is not None:
        return org_id
    if org_slug:
        org = db.query(Organization).filter(Organization.slug == org_slug).first()
        if not org:
            sys.exit(f"No organization with slug '{org_slug}'.")
        return org.id

    # Auto-pick the organization with the most recording sessions.
    row = (
        db.query(
            RecordingSession.organization_id,
            func.count(RecordingSession.id).label("n"),
        )
        .group_by(RecordingSession.organization_id)
        .order_by(func.count(RecordingSession.id).desc())
        .first()
    )
    if not row or row[0] is None:
        sys.exit("No recording sessions found in any organization — nothing to eval.")
    return row[0]


async def _run_query(db, org_id, query, limit, graph_augment):
    from services.agent_tools import search_meetings_impl

    result = await search_meetings_impl(
        db,
        org_id,
        query=query,
        limit=limit,
        graph_augment=graph_augment,
    )
    return result


def _top(results, n=3):
    return [
        {
            "session_id": str(r.get("session_id")),
            "title": r.get("title") or "(untitled)",
            "score": float(r.get("score") or 0.0),
            "match_type": r.get("match_type", ""),
            "graph_bonus": float(r.get("graph_bonus") or 0.0),
        }
        for r in (results or [])[:n]
    ]


def _reordered(off_rows, on_rows) -> bool:
    return [r["session_id"] for r in off_rows] != [r["session_id"] for r in on_rows]


def _print_block(query, off, on, off_engaged, on_engaged):
    print("=" * 110)
    print(f"QUERY: {query}")
    print("-" * 110)
    header = f"{'#':<2} {'graph_augment=OFF':<{TITLE_WIDTH + 12}}  |  {'graph_augment=ON':<{TITLE_WIDTH + 12}}"
    print(header)
    print("-" * 110)
    rows = max(len(off["top"]), len(on["top"]))
    if rows == 0:
        print("   (no results in either mode)")
    for i in range(rows):
        loff = ""
        if i < len(off["top"]):
            r = off["top"][i]
            loff = f"[{r['score']:+.3f}] {_truncate(r['title'])}"
        lon = ""
        if i < len(on["top"]):
            r = on["top"][i]
            gb = f" (+g {r['graph_bonus']:.2f})" if r["graph_bonus"] else ""
            lon = f"[{r['score']:+.3f}] {_truncate(r['title'])}{gb}"
        print(f"{i + 1:<2} {loff:<{TITLE_WIDTH + 12}}  |  {lon:<{TITLE_WIDTH + 12}}")
    print("-" * 110)
    flag = "YES" if _reordered(off["top"], on["top"]) else "no"
    print(
        f"reorder happened: {flag}   |   "
        f"OFF match_type={off['match_type']}   |   "
        f"ON match_type={on['match_type']} (graph engaged: {'YES' if on_engaged else 'no'})"
    )
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", type=int, default=None, help="Organization id to query.")
    parser.add_argument("--org-slug", type=str, default=None, help="Organization slug to query.")
    parser.add_argument("--limit", type=int, default=5, help="Search limit per query (default 5).")
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        default=None,
        help="Override query (repeatable). Defaults to the four anchor queries.",
    )
    args = parser.parse_args(argv)

    from database.database import SessionLocal

    queries = args.queries or ANCHOR_QUERIES

    db = SessionLocal()
    try:
        org_id = _resolve_org_id(db, args.org_id, args.org_slug)
        print()
        print(f"graph-RAG A/B eval  |  org_id={org_id}  |  limit={args.limit}  |  {len(queries)} queries")
        print()

        reorder_count = 0
        engaged_count = 0
        for query in queries:
            off_res = asyncio.run(_run_query(db, org_id, query, args.limit, False))
            on_res = asyncio.run(_run_query(db, org_id, query, args.limit, True))
            off = {"top": _top(off_res["results"], 3), "match_type": off_res.get("match_type", "")}
            on = {"top": _top(on_res["results"], 3), "match_type": on_res.get("match_type", "")}
            on_engaged = on_res.get("match_type") == "graph_augmented" and "graph" in on_res
            if on_engaged:
                engaged_count += 1
            if _reordered(off["top"], on["top"]):
                reorder_count += 1
            _print_block(query, off, on, off_res.get("match_type", ""), on_engaged)

        print("=" * 110)
        print(
            f"SUMMARY: {reorder_count}/{len(queries)} queries reordered  |  "
            f"graph augmentation engaged on {engaged_count}/{len(queries)} queries"
        )
        print("=" * 110)
    finally:
        db.close()


if __name__ == "__main__":
    main()
