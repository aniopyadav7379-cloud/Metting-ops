#!/usr/bin/env python3
"""Bounded, single-workspace Project-Ops action-item reconciliation.

This command has no all-tenant default. An operator must name the local
Meeting-Ops organization id, and each run is capped at 100 rows.
"""

from __future__ import annotations

import argparse
import asyncio

from database.database import SessionLocal
from services.projectops_lifecycle import (
    MAX_RECONCILE_ITEMS,
    reconcile_projectops_action_items,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--organization-id",
        required=True,
        type=int,
        help="Meeting-Ops organization id to reconcile (required tenant fence)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        choices=range(1, MAX_RECONCILE_ITEMS + 1),
        metavar="1..100",
    )
    return parser.parse_args()


async def _run(organization_id: int, limit: int) -> int:
    db = SessionLocal()
    try:
        result = await reconcile_projectops_action_items(
            db=db,
            organization_id=organization_id,
            limit=limit,
        )
        print(
            "Project-Ops lifecycle reconciliation "
            f"organization_id={organization_id} requested={result['requested']} "
            f"reconciled={result['reconciled']} failed={result['failed']}"
        )
        return 1 if result["failed"] else 0
    finally:
        db.close()


def main() -> int:
    args = _args()
    return asyncio.run(_run(args.organization_id, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
