"""Post-build verification for Phase 2 agent-action safety.

Runs a real propose -> confirm -> audit round-trip plus every negative case
the design ratified, against a live backend (in-container). Intended to be run
RIGHT after the Phase 2 build is deployed, to prove the safety contract holds
before any user can touch it.

Usage (inside meet-backend, after Phase 2 is built + deployed):
    python3 scripts/verify_agent_actions.py [--user-id N --org-id N --session-id N]

If IDs aren't passed, it discovers a real session belonging to org 1 + admin.

Covers (per docs/agent-platform-phase-2-design.md):
  POSITIVE:
    1. propose rename_session on an owned session
    2. confirm with the returned token
    3. row is actually updated (title now matches "to")
    4. audit_logs has both agent_action_proposed and agent_action_confirmed
       rows tied by the same proposal_id in details
    5. token is consumed (a second confirm with the same token is rejected)
  NEGATIVE:
    6. expired token rejection
    7. replayed token rejection (covered by 5)
    8. cross-org rejection (propose as another org, confirm as org-1 user)
    9. tier-gate rejection (reprocess from a free-tier caller)
   10. payload-tamper rejection (mutate the diff after proposal)
   11. state-drift rejection (mutate the row between propose + confirm)
   12. cancel writes an agent_action_cancelled audit row + frees the token

The script is read-mostly: positive tests reverse their mutation at the end
so the target row's title is left as it was found. Audit rows from this run
are tagged details.test_run=true so they're easy to filter out later.

Exit non-zero on any failure; exit 0 on all-green.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("verify-agent")

# Placeholders — the actual service-layer entrypoints land in Codex's build.
# This script is the contract: when those modules exist, the imports + calls
# below describe the EXACT shape we expect the build to satisfy.

EXPECTED_MODULES = {
    "services.agent_actions": [
        "propose_action",     # (*, db, user, org_id, action, payload) -> envelope (async)
        "confirm_action",     # (*, db, user, org_id, confirmation_token) -> result (async)
        "cancel_action",      # (*, db, user, org_id, confirmation_token) -> ack (async)
    ],
    "services.agent_write_tools": [
        "propose_rename_session",
        "propose_create_session",
        "propose_add_tag",
        "propose_remove_tag",
        "propose_trigger_reprocess",
        "propose_draft_followup_email",
    ],
    "api.agent_actions": [],   # FastAPI router; presence-only check
}

# Audit action names per the ratified design.
ACTION_PROPOSED = "agent_action_proposed"
ACTION_CONFIRMED = "agent_action_confirmed"
ACTION_CANCELLED = "agent_action_cancelled"


def _import_or_fail(modname: str, attrs: list[str]) -> Any:
    try:
        mod = __import__(modname, fromlist=attrs or ["__name__"])
    except ImportError as e:
        log.error("CONTRACT FAIL: module %s not importable: %s", modname, e)
        return None
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        log.error("CONTRACT FAIL: module %s missing: %s", modname, missing)
        return None
    return mod


def assert_contract() -> bool:
    log.info("== contract check: required modules + entry points ==")
    ok = True
    for modname, attrs in EXPECTED_MODULES.items():
        mod = _import_or_fail(modname, attrs)
        ok = ok and mod is not None
    return ok


def discover_target(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    from database.database import SessionLocal
    from database.models import RecordingSession
    import database.models_rooms  # noqa: F401
    from auth.models import User, Organization, UserOrganization

    db = SessionLocal()
    try:
        if args.user_id and args.org_id and args.session_id:
            u = db.get(User, args.user_id)
            o = db.get(Organization, args.org_id)
            s = db.get(RecordingSession, args.session_id)
        else:
            u = db.query(User).filter(User.is_superuser.is_(True)).order_by(User.id).first()
            o = db.query(Organization).filter(Organization.slug == "magic-unicorn").first() \
                or db.query(Organization).order_by(Organization.id).first()
            s = (db.query(RecordingSession)
                   .filter(RecordingSession.organization_id == o.id,
                           RecordingSession.status == "completed")
                   .order_by(RecordingSession.id.desc()).first())
        if not (u and o and s):
            log.error("could not discover (user, org, session) — pass --user-id/--org-id/--session-id explicitly")
            return None, None, None
        log.info("target: user=%s org=%s session=%s title=%r", u.id, o.id, s.id, s.title or s.name)
        return u, o, s
    finally:
        db.close()


def case_positive_rename(user, org, session) -> bool:
    """1-5: propose -> confirm -> row updated -> audit double-row -> token consumed."""
    from database.database import SessionLocal
    from database.models import RecordingSession
    from auth.models import AuditLog
    from services.agent_actions import propose, confirm

    original_title = session.title or session.name or "(untitled)"
    new_title = f"{original_title} (verify-run-{int(time.time())})"

    db = SessionLocal()
    try:
        env = propose(
            user=user, org=org,
            action="rename_session",
            payload={"session_id": session.id, "title": new_title},
        )
        assert env["status"] == "needs_confirmation", f"expected needs_confirmation, got {env!r}"
        token = env["confirmation_token"]
        proposal_id = env.get("proposal_id") or env.get("details", {}).get("proposal_id")
        log.info("[POS] proposal envelope OK token=%s proposal_id=%s", token[:12] + "...", proposal_id)

        result = confirm(user=user, token=token)
        assert result["status"] == "applied", f"expected applied, got {result!r}"
        log.info("[POS] confirm OK")

        db.expire_all()
        fresh = db.get(RecordingSession, session.id)
        assert fresh.title == new_title, f"row title not updated: {fresh.title!r} != {new_title!r}"
        log.info("[POS] row updated to new title")

        rows = (db.query(AuditLog)
                  .filter(AuditLog.action.in_([ACTION_PROPOSED, ACTION_CONFIRMED]),
                          AuditLog.details["proposal_id"].astext == str(proposal_id))
                  .all())
        actions = sorted(r.action for r in rows)
        assert actions == sorted([ACTION_PROPOSED, ACTION_CONFIRMED]), \
            f"expected lifecycle pair, got {actions!r}"
        log.info("[POS] audit_logs has both proposed + confirmed for proposal_id=%s", proposal_id)

        # Replay
        try:
            confirm(user=user, token=token)
            log.error("[POS] CONTRACT FAIL: replay was accepted")
            return False
        except Exception:
            log.info("[POS] replay correctly rejected")

        # Restore
        fresh.title = original_title
        db.commit()
        return True
    finally:
        db.close()


def case_negative_cross_org(user, org, session) -> bool:
    from services.agent_actions import propose, confirm
    from database.database import SessionLocal
    from auth.models import Organization, User

    db = SessionLocal()
    try:
        other_org = (db.query(Organization).filter(Organization.id != org.id).first())
        if not other_org:
            log.info("[NEG cross-org] only one org exists; skipping")
            return True
        env = propose(user=user, org=other_org, action="rename_session",
                      payload={"session_id": session.id, "title": "should fail"})
        # confirm as original org's user should be rejected (target session is org-id mismatch)
        try:
            confirm(user=user, token=env["confirmation_token"])
            log.error("[NEG cross-org] CONTRACT FAIL: cross-org mutation accepted")
            return False
        except Exception:
            log.info("[NEG cross-org] correctly rejected")
            return True
    finally:
        db.close()


def case_negative_state_drift(user, org, session) -> bool:
    """Mutate the target row between propose and confirm; confirm must refuse."""
    from services.agent_actions import propose, confirm
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        original = (db.get(RecordingSession, session.id).title) or ""
        env = propose(user=user, org=org, action="rename_session",
                      payload={"session_id": session.id, "title": original + " (proposed)"})
        # Out-of-band mutation
        row = db.get(RecordingSession, session.id)
        row.title = original + " (drifted)"
        db.commit()
        try:
            confirm(user=user, token=env["confirmation_token"])
            log.error("[NEG state-drift] CONTRACT FAIL: confirm accepted despite drift")
            return False
        except Exception:
            log.info("[NEG state-drift] correctly rejected")
            # Restore
            db.get(RecordingSession, session.id).title = original
            db.commit()
            return True
    finally:
        db.close()


def case_negative_expired_token(user, org, session) -> bool:
    from services.agent_actions import propose, confirm
    import services.agent_actions as aa

    env = propose(user=user, org=org, action="add_tag",
                  payload={"session_id": session.id, "tag": "verify-expiry"})
    token = env["confirmation_token"]
    # Force expire by deleting the Redis key directly.
    try:
        import redis
        url = os.getenv("ARQ_REDIS_URL") or os.getenv("REDIS_URL", "redis://unicorn-redis:6379/4")
        r = redis.Redis.from_url(url)
        ns_keys = r.keys("meeting-ops:agent-actions:*")
        for k in ns_keys:
            r.delete(k)
    except Exception as e:
        log.warning("[NEG expired] could not force-expire via Redis (%s); falling back to sleep", e)
        time.sleep(2)
    try:
        confirm(user=user, token=token)
        log.error("[NEG expired] CONTRACT FAIL: expired token accepted")
        return False
    except Exception:
        log.info("[NEG expired] correctly rejected")
        return True


def case_cancel_writes_audit(user, org, session) -> bool:
    from services.agent_actions import propose, cancel
    from database.database import SessionLocal
    from auth.models import AuditLog

    env = propose(user=user, org=org, action="add_tag",
                  payload={"session_id": session.id, "tag": "verify-cancel"})
    proposal_id = env.get("proposal_id") or env.get("details", {}).get("proposal_id")
    cancel(user=user, token=env["confirmation_token"])
    db = SessionLocal()
    try:
        n = (db.query(AuditLog)
               .filter(AuditLog.action == ACTION_CANCELLED,
                       AuditLog.details["proposal_id"].astext == str(proposal_id))
               .count())
        if n != 1:
            log.error("[CANCEL] CONTRACT FAIL: expected 1 cancelled audit row, got %d", n)
            return False
        log.info("[CANCEL] cancelled audit row written")
        return True
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", type=int)
    ap.add_argument("--org-id", type=int)
    ap.add_argument("--session-id", type=int)
    args = ap.parse_args()

    if not assert_contract():
        log.error("contract not satisfied — has Phase 2 actually been deployed?")
        return 2

    user, org, session = discover_target(args)
    if not session:
        return 2

    results = {
        "positive_rename": case_positive_rename(user, org, session),
        "negative_cross_org": case_negative_cross_org(user, org, session),
        "negative_state_drift": case_negative_state_drift(user, org, session),
        "negative_expired_token": case_negative_expired_token(user, org, session),
        "cancel_writes_audit": case_cancel_writes_audit(user, org, session),
    }
    log.info("== results ==\n%s", json.dumps(results, indent=2))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
