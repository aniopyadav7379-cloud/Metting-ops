"""Arq worker: always-on session finalize — v3.18.3.

Moves the body of `POST /sessions/{id}/finalize` (`finalize_always_on_session`
in `api/recording.py`) into an arq function so the HTTP handler can
return 202 immediately and the long-running summary + insights + Brigade
+ Project-Ops fan-out happens off the request path. The previous in-band
implementation could spend 60-180 seconds on the LLM round-trip alone,
which combined with Cloudflare's 100s edge timeout (CF docs say 100, 524
fires at 100) made the always-on Stop flow unreliable on real meetings.

Drift safety: the worker re-reads the session at the start of its run
and compares `processing_job_id` against its own ctx job_id. If a newer
job is now processing the row (user clicked Stop twice, or operator
triggered a manual reprocess), this worker skips the side effects and
returns. This avoids double-writes to Brigade / Project-Ops and prevents
a stale worker from overwriting a fresher summary.

Idempotency on the worker side complements the in-process idempotency
window in `services.job_runner` — the cache catches fast double-clicks
inside the same uvicorn, the drift check catches races across processes
or longer time gaps.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from arq import Retry

logger = logging.getLogger(__name__)


async def finalize_session_job(
    ctx: dict[str, Any],
    session_pk: int,
    user_id: int | None = None,
    org_id: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run the always-on finalize pipeline for a session.

    Steps (matching the legacy in-band code path):
      1. Server summary (`_summarize_session` -> writes session.summary).
      2. AI insights (`_generate_ai_insights` -> writes session.ai_insights).
      3. Mark status=completed.
      4. Brigade graph write (fire-and-forget, per-meeting node + relationships).
      5. Project-Ops action-item bridge (fire-and-forget).

    Drift check fires at step 1 entry — if `processing_job_id` no longer
    matches the arq job_id we're running, a newer job claimed the row
    and we bail before touching summary / insights / Brigade / PO.
    """
    from middleware.request_context import bind_request_id
    bind_request_id(request_id or ctx.get("job_id"))
    from database.database import SessionLocal
    from database.models import RecordingSession, Transcription

    own_job_id = ctx.get("job_id") if isinstance(ctx, dict) else None

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        if not session:
            logger.warning("finalize_session_job: session %s gone", session_pk)
            return {"status": "skipped", "reason": "session_missing", "session_pk": session_pk}

        # Drift check: if a newer job_id stamped the row, bail.
        current_job_id = getattr(session, "processing_job_id", None)
        if own_job_id and current_job_id and current_job_id != own_job_id:
            logger.warning(
                "finalize_session_job: drift on session=%s own=%s current=%s — skipping",
                session_pk, own_job_id, current_job_id,
            )
            return {
                "status": "skipped",
                "reason": "drift",
                "session_pk": session_pk,
                "own_job_id": own_job_id,
                "current_job_id": current_job_id,
            }

        # ---- Step 0: identify enrolled speakers + normalize labels ----
        # This step was MISSING from the live finalize path (it only existed
        # in the reprocess pipeline), so an always-on meeting was summarized
        # with raw pyannote labels even when the voices were enrolled — the
        # exact "SPEAKER_02 said…" summaries Aaron hit. Identify first, then
        # normalize leftovers to "Speaker N" + resync the transcript rows the
        # UI reads. Best-effort: a speaker-svc outage must not fail finalize.
        try:
            import asyncio as _asyncio
            from services.speaker_service import (
                identify_speakers,
                stamp_confirmed_speaker_contacts,
            )
            await _asyncio.to_thread(identify_speakers, session, db)
            await _asyncio.to_thread(stamp_confirmed_speaker_contacts, session, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize_session_job: identify_speakers failed session=%s: %s",
                session_pk, exc,
            )
        try:
            from services.speaker_labels import normalize_session_speaker_labels
            normalize_session_speaker_labels(session, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize_session_job: label normalization failed session=%s: %s",
                session_pk, exc,
            )

        # ---- Step 1: server summary ----
        summary_error: str | None = None
        try:
            from api.uploads import _summarize_session
            await _summarize_session(db, session, template="standard")
        except Exception as exc:  # noqa: BLE001
            summary_error = str(exc)[:500]
            logger.exception("finalize_session_job: summary failed session=%s", session_pk)
            metadata = dict(session.processing_metadata or {})
            metadata["needs_summary"] = True
            metadata["summary_error"] = summary_error
            session.processing_metadata = metadata
            session.status = "processing"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(session, "processing_metadata")
            db.commit()
            from services.transient_errors import is_transient_error, retry_transient
            if is_transient_error(exc):
                await retry_transient(ctx, exc)
            return {
                "status": "failed",
                "session_pk": session_pk,
                "error": summary_error,
                "retryable": True,
            }

        # ---- Step 1b: auto-title fallback ----
        # _summarize_session only assigns a real title when it successfully
        # summarizes. Always-on sessions whose transcript is too short to
        # summarize (or whose summarizer LLM failed) keep the default
        # "Always-on YYYY-MM-DD HH:MM" title forever. Here we guarantee a
        # real title for every finalized session that has any transcript:
        # if the title is still a default placeholder AND the user hasn't
        # set one, generate a concise title straight from the transcript via
        # the same org-aware "fast" LLM route. Best-effort and fully
        # isolated — a title failure must never fail finalize.
        title_error: str | None = None
        try:
            from services.unified_agent_service import (
                generate_title_from_transcript,
                is_default_session_title,
            )

            transcript_for_title = (
                session.transcript_simple or session.transcript or ""
            ).strip()
            if (
                transcript_for_title
                and not getattr(session, "title_user_set", False)
                and is_default_session_title(getattr(session, "title", None))
            ):
                generated = await generate_title_from_transcript(
                    session.organization_id,
                    transcript_for_title,
                )
                # Re-check the guard: _summarize_session may have set a real
                # title concurrently in the same session object above.
                if generated and is_default_session_title(getattr(session, "title", None)):
                    session.title = generated
                    if not (session.name or "").strip() or is_default_session_title(
                        getattr(session, "name", None)
                    ):
                        session.name = generated
                    logger.info(
                        "finalize_session_job: auto-titled session=%s -> %r",
                        session_pk, generated,
                    )
        except Exception as exc:  # noqa: BLE001
            title_error = str(exc)[:500]
            logger.warning(
                "finalize_session_job: auto-title failed session=%s: %s",
                session_pk, exc,
            )

        # ---- Step 2: AI insights ----
        insights_error: str | None = None
        try:
            from api.ai_insights import _generate_ai_insights
            transcriptions = (
                db.query(Transcription)
                .filter(Transcription.session_id == session.id)
                .order_by(Transcription.start_time.asc())
                .all()
            )
            full_text = (session.transcript_simple or session.transcript or "").strip()
            insights = await _generate_ai_insights(
                full_text,
                transcriptions,
                session,
                db,
                session.organization_id,
            )
            session.ai_insights = insights.model_dump()
        except Exception as exc:  # noqa: BLE001
            insights_error = str(exc)[:500]
            logger.warning(
                "finalize_session_job: insights failed session=%s: %s",
                session_pk, exc,
            )

        # ---- Step 2b: semantic index (Qdrant) ----
        # v3.34.0 (audit finding #1): this job NEVER indexed, so (a) live
        # always-on meetings stayed invisible to cross-meeting search + RAG
        # until a manual reprocess, and (b) the v3.33.x rename-triggered
        # refresh regenerated the summary but left STALE labels/title/summary
        # in the Qdrant chunks forever. index_session delete-then-rewrites
        # per session_id, so this also heals renames. Mirrors the reprocess
        # Stage 5.9. Best-effort + off-thread.
        try:
            import asyncio as _asyncio2
            from services.semantic_search_service import semantic_search

            _segs = (
                session.transcript_diarized.get("segments")
                if isinstance(session.transcript_diarized, dict)
                else None
            )
            if _segs:
                # Normalize through the shared helper so indexed snippets /
                # citations carry "Speaker N" / real names, never raw SPEAKER_00
                # codes — even on an edge path that skipped the normalize step.
                from services.speaker_labels import build_attributed_transcript
                _index_transcript, _ = build_attributed_transcript(_segs)
            else:
                _index_transcript = session.transcript_simple or session.transcript or ""
            _index_summary = session.summary or ""
            if not _index_summary and isinstance(session.final_summary, dict):
                _index_summary = (
                    session.final_summary.get("executive")
                    or session.final_summary.get("summary")
                    or ""
                )
            if (_index_transcript or "").strip():
                await _asyncio2.to_thread(
                    semantic_search.index_session,
                    session_id=session.session_id or str(session.id),
                    title=session.title or session.name or "",
                    transcript=_index_transcript,
                    summary=_index_summary,
                    created_at=session.created_at.isoformat() if session.created_at else "",
                    organization_id=session.organization_id,
                )
                logger.info(
                    "finalize_session_job: semantic index updated session=%s (%d segs)",
                    session_pk, len(_segs) if _segs else 0,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize_session_job: semantic index failed (non-fatal) session=%s: %s",
                session_pk, exc,
            )

        # ---- Step 3: mark completed + stamp completion ----
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            session.status = "completed"
            metadata = dict(session.processing_metadata or {})
            metadata["completion"] = {
                "completed_at": now.isoformat(),
                "job_id": own_job_id,
                "summary_error": summary_error,
                "insights_error": insights_error,
                "title_error": title_error,
            }
            session.processing_metadata = metadata
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(session, "processing_metadata")
            session.updated_at = now
            db.commit()
            db.refresh(session)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "finalize_session_job: failed to mark completed session=%s",
                session_pk,
            )
            # If we can't even mark completed, give up — caller polling
            # /api/jobs/<id> will see failed status.
            return {
                "status": "failed",
                "session_pk": session_pk,
                "error": str(exc)[:500],
            }

        # ---- Step 4: Brigade graph write (best-effort) ----
        try:
            from services.brigade_client import BrigadeClient
            from services.brigade_writer import write_meeting_to_brigade

            brigade = BrigadeClient()
            await write_meeting_to_brigade(
                session_pk,
                db,
                client=brigade,
                completion_mode="live",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize_session_job: brigade write failed session=%s: %s",
                session_pk, exc,
            )

        # ---- Step 5: Project-Ops triage submit (best-effort) ----
        # Action items go to the PO triage inbox (propose-only); a human
        # approves in Project-Ops before anything becomes a task.
        try:
            from services.projectops_writer import submit_action_items_to_triage
            _po_result = await submit_action_items_to_triage(
                db=db, session_pk=session_pk, completion_mode="live"
            )
            if not _po_result.ok:
                logger.warning(
                    "finalize_session_job: projectops triage push did NOT "
                    "succeed session=%s mode=%s detail=%s",
                    session_pk, _po_result.mode, _po_result.detail,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize_session_job: projectops write failed session=%s: %s",
                session_pk, exc,
            )

        return {
            "status": "completed",
            "session_pk": session_pk,
            "session_id": session.session_id,
            "summary_error": summary_error,
            "insights_error": insights_error,
            "title_error": title_error,
        }
    except asyncio.CancelledError:
        raise
    except Retry:
        raise
    except Exception as exc:
        logger.exception(
            "finalize_session_job: unexpected crash session=%s", session_pk,
        )
        from services.transient_errors import is_transient_error, retry_transient
        if is_transient_error(exc):
            await retry_transient(ctx, exc)
        return {
            "status": "failed",
            "session_pk": session_pk,
            "error": str(exc)[:500],
        }
    finally:
        db.close()
