"""
Automatic Summarization Service
Handles backend-controlled periodic summarization during live recording.

LLM calls route exclusively through the org-aware ProviderRegistry. When no
org context can be resolved or the provider call fails the service degrades
gracefully (no summary) rather than silently substituting a default provider —
silent fallback would bypass per-org provider configuration and billing.
"""

import asyncio
import time
import logging
from typing import Dict, Optional, Any
from datetime import datetime, timezone
import json

from services.system_service import SystemService
from services.providers import get_provider_registry

logger = logging.getLogger(__name__)


def _resolve_org_id(session_id: str) -> Optional[int]:
    """Look up organization_id for a session_id (uuid string) from the DB."""
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession

        db = SessionLocal()
        try:
            row = (
                db.query(RecordingSession.organization_id)
                .filter(RecordingSession.session_id == session_id)
                .first()
            )
            if row and row[0] is not None:
                return int(row[0])
        finally:
            db.close()
    except Exception as exc:
        logger.debug(f"Could not resolve org_id for session {session_id}: {exc}")
    return None


def _load_session_transcript(session_id: str) -> str:
    """Load the canonical live transcript text for a session.

    Reads `RecordingSession.transcript_simple`, which is populated by both
    the browser-STT chunks-text path (`/api/recordings/sessions/{id}/chunks-text`,
    landed in bf345ec) and the legacy appliance whisper path
    (`_append_transcription` in `api/recording.py`). Returns "" on any
    missing/empty/error state so callers can treat absence uniformly.

    Replaces the legacy in-memory `live_recording_transcription.transcript_buffer`
    read, which only worked on the appliance whisper-server code path and was
    empty in cloud builds (where DISABLE_LOCAL_AUDIO=1 keeps that service inert).
    """
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession

        db = SessionLocal()
        try:
            row = (
                db.query(RecordingSession.transcript_simple)
                .filter(RecordingSession.session_id == session_id)
                .first()
            )
            if row and row[0]:
                return str(row[0])
        finally:
            db.close()
    except Exception as exc:
        logger.debug(f"Could not load transcript for session {session_id}: {exc}")
    return ""


def _persist_final_summary(session_id: str, final_summary: Dict[str, Any]) -> bool:
    """Persist a final summary on the canonical recording-session row.

    Auto-summarization used to write to a legacy in-memory ``sessions`` map
    that no longer exists.  That NameError was caught by the broad outer
    handler in ``generate_final_summary``, making a successfully generated
    summary silently disappear.  Keep the write adjacent to the canonical
    transcript read and make the commit result explicit to callers.
    """
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession

        db = SessionLocal()
        try:
            session = (
                db.query(RecordingSession)
                .filter(RecordingSession.session_id == session_id)
                .first()
            )
            if not session:
                logger.warning(
                    "Cannot persist final summary: recording session %s was not found",
                    session_id,
                )
                return False

            session.final_summary = final_summary
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception:
        logger.exception("Could not persist final summary for session %s", session_id)
        return False


_SUMMARY_PROMPTS = {
    "executive": (
        "Create an executive summary with:\n"
        "1. Key Decisions (bullet points)\n"
        "2. Action Items with owners\n"
        "3. Strategic Implications\n"
        "4. Next Steps\n"
        "Keep it concise and actionable."
    ),
    "technical": (
        "Create a technical summary with:\n"
        "1. Technical Topics Discussed\n"
        "2. Architecture Decisions\n"
        "3. Implementation Details\n"
        "4. Technical Debt Items\n"
        "5. Dependencies and Blockers"
    ),
    "standard": (
        "Write the meeting brief from the supplied transcript now. Do not ask for "
        "a recording, another transcript, more context, or a summary; do not explain "
        "your process. Use only transcript facts. Include:\n"
        "1. Main Topics (3-5 concrete bullet points)\n"
        "2. Key Decisions Made\n"
        "3. Action Items (who, what, when; use 'owner not stated' when absent)\n"
        "4. Open Questions and Risks\n"
        "Write 'None noted' for an empty section. Keep it clear, specific, and organized."
    ),
}


def _parse_summary(response: str) -> Dict[str, Any]:
    """Heuristic parse of an LLM summary into structured sections + lists."""
    summary: Dict[str, Any] = {
        "summary": response,
        "sections": {},
        "action_items": [],
        "key_decisions": [],
    }
    current_section: Optional[str] = None
    current_content: list = []

    for raw_line in response.split("\n"):
        line = raw_line.strip()
        lower = line.lower()
        if any(h in lower for h in ("action items", "action points")):
            if current_section:
                summary["sections"][current_section] = "\n".join(current_content)
            current_section, current_content = "action_items", []
        elif any(h in lower for h in ("decisions", "key decisions")):
            if current_section:
                summary["sections"][current_section] = "\n".join(current_content)
            current_section, current_content = "decisions", []
        elif any(h in lower for h in ("topics", "main topics")):
            if current_section:
                summary["sections"][current_section] = "\n".join(current_content)
            current_section, current_content = "topics", []
        elif line and not line.endswith(":"):
            current_content.append(line)

    if current_section and current_content:
        summary["sections"][current_section] = "\n".join(current_content)

    action_block = summary["sections"].get("action_items", "")
    if action_block:
        summary["action_items"] = [
            line.strip("- *").strip()
            for line in action_block.split("\n")
            if line.strip() and line.strip()[0] in "-*"
        ]
    decisions_block = summary["sections"].get("decisions", "")
    if decisions_block:
        summary["key_decisions"] = [
            line.strip("- *").strip()
            for line in decisions_block.split("\n")
            if line.strip() and line.strip()[0] in "-*"
        ]
    return summary


def _summarize_with_org(
    transcript: str,
    org_id: Optional[int],
    task: str = "fast",
    template: str = "standard",
) -> Optional[Dict[str, Any]]:
    """Summarize a transcript via the org's configured LLM provider.

    Returns a parsed dict ({summary, sections, action_items, key_decisions})
    on success, or None on any failure. No silent provider substitution: if
    the registry can't resolve a provider (or the call fails) the caller
    degrades gracefully without LLM-derived structure.
    """
    if org_id is None:
        logger.debug("No org context for summary, skipping LLM call")
        return None

    prompt_body = _SUMMARY_PROMPTS.get(template, _SUMMARY_PROMPTS["standard"])

    try:
        from database.database import SessionLocal
        db = SessionLocal()
        try:
            registry = get_provider_registry(db)
            provider = registry.get_llm(org_id=org_id, task=task)
        finally:
            db.close()

        text = provider.chat_sync(
            system_prompt=(
                "You are a precise meeting-notes writer. The supplied transcript is the "
                "source material: produce the finished brief, never a request for more "
                "material or instructions for the user."
            ),
            user_prompt=f"{prompt_body}\n\nTranscript:\n{transcript[:16000]}",
            max_tokens=500,
            temperature=0.5,
        )
    except Exception as exc:
        logger.error(
            f"ProviderRegistry summarize failed for org {org_id} task={task}: {exc}",
            exc_info=True,
        )
        return None

    if not text:
        return None
    return _parse_summary(text)


class AutoSummarizationService:
    """Service for automatic summarization during recording"""

    def __init__(self):
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.last_summary_time: Dict[str, float] = {}
        self.last_transcript_length: Dict[str, int] = {}
        self.last_summary_word_count: Dict[str, int] = {}  # Track word count at last summary
        self.total_word_count: Dict[str, int] = {}  # Track total words processed
        self.summaries_cache: Dict[str, Dict[str, Any]] = {}
        self.summary_history: Dict[str, list] = {}  # Store all summaries for final compilation
        self.session_org_id: Dict[str, Optional[int]] = {}  # Per-session resolved org id
        
    async def start_auto_summarization(
        self,
        session_id: str,
        websocket_callback=None,
        organization_id: Optional[int] = None,
    ) -> None:
        """
        Start automatic summarization for a session.

        Args:
            session_id: Recording session ID
            websocket_callback: Async function to send updates via WebSocket
            organization_id: Optional active org id used to route LLM calls
                through the per-org ProviderRegistry. When None, the service
                resolves org_id from the RecordingSession row, falling back to
                the legacy unified LLM service if neither is available.
        """
        # Check if already running for this session
        if session_id in self.active_tasks:
            logger.warning(f"Auto-summarization already running for session {session_id}")
            return

        # Cache the org id (or resolve it lazily later)
        if organization_id is not None:
            self.session_org_id[session_id] = organization_id
        else:
            self.session_org_id[session_id] = await asyncio.to_thread(_resolve_org_id, session_id)

        # Create and store the task
        task = asyncio.create_task(
            self._summarization_loop(session_id, websocket_callback)
        )
        self.active_tasks[session_id] = task
        logger.info(
            f"Started auto-summarization for session {session_id} "
            f"(org_id={self.session_org_id.get(session_id)})"
        )
        
    async def stop_auto_summarization(self, session_id: str) -> None:
        """Stop automatic summarization for a session"""
        if session_id in self.active_tasks:
            task = self.active_tasks[session_id]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self.active_tasks[session_id]

            # Clean up tracking data
            self.last_summary_time.pop(session_id, None)
            self.last_transcript_length.pop(session_id, None)
            self.session_org_id.pop(session_id, None)

            logger.info(f"Stopped auto-summarization for session {session_id}")
    
    async def _summarization_loop(
        self, 
        session_id: str,
        websocket_callback=None
    ) -> None:
        """
        Main loop for automatic summarization
        
        Checks settings and generates summaries based on configured trigger mode
        """
        try:
            while True:
                # Load current settings
                settings = SystemService.load_settings()
                
                # Check if live summarization is enabled
                if not settings.enableLiveSummarization:
                    logger.info(f"Live summarization disabled for session {session_id}")
                    await asyncio.sleep(10)  # Check settings every 10 seconds
                    continue
                
                # Determine if we should generate a summary
                should_generate = False
                trigger_reason = ""
                next_update_value = None
                
                if settings.summaryTriggerMode == "word_count":
                    # Word count based triggering
                    current_word_count = await self._get_current_word_count(session_id)
                    last_summary_words = self.last_summary_word_count.get(session_id, 0)
                    word_diff = current_word_count - last_summary_words
                    
                    if word_diff >= settings.summaryWordCountInterval:
                        should_generate = True
                        trigger_reason = f"word_count ({word_diff} new words)"
                        next_update_value = f"in {settings.summaryWordCountInterval} words"
                        
                else:  # time mode
                    # Time-based triggering (legacy mode)
                    current_time = time.time()
                    last_time = self.last_summary_time.get(session_id, 0)
                    
                    if current_time - last_time >= settings.summaryUpdateInterval:
                        should_generate = True
                        trigger_reason = f"time ({settings.summaryUpdateInterval}s elapsed)"
                        next_update_value = f"in {settings.summaryUpdateInterval} seconds"
                
                if should_generate:
                    # Generate summary
                    summary = await self._generate_summary(session_id)
                    
                    if summary:
                        # Cache the summary
                        self.summaries_cache[session_id] = summary
                        
                        # Store in history for final summary
                        if session_id not in self.summary_history:
                            self.summary_history[session_id] = []
                        self.summary_history[session_id].append(summary)
                        
                        # Update tracking based on mode
                        if settings.summaryTriggerMode == "word_count":
                            current_words = await self._get_current_word_count(session_id)
                            self.last_summary_word_count[session_id] = current_words
                        else:
                            self.last_summary_time[session_id] = time.time()
                        
                        # Send via WebSocket if callback provided
                        if websocket_callback:
                            await websocket_callback({
                                "type": "auto_summary",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "summary": summary,
                                "trigger_mode": settings.summaryTriggerMode,
                                "trigger_reason": trigger_reason,
                                "next_update": next_update_value,
                                "word_count": self.total_word_count.get(session_id, 0)
                            })
                        
                        logger.info(f"Generated auto-summary for session {session_id} (trigger: {trigger_reason})")
                
                # Sleep for a short time before checking again
                await asyncio.sleep(5)  # Check every 5 seconds
                
        except asyncio.CancelledError:
            logger.info(f"Summarization loop cancelled for session {session_id}")
            raise
        except Exception as e:
            logger.error(f"Error in summarization loop for session {session_id}: {e}")
            # Clean up on error
            self.active_tasks.pop(session_id, None)
    
    async def _generate_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Generate a summary for the current transcript
        
        Returns summary data or None if transcript hasn't changed significantly
        """
        try:
            # Get the current transcript from the canonical DB column. This
            # covers browser-STT (cloud + always_on) and the legacy appliance
            # whisper path, both of which write transcript_simple.
            transcript_text = (
                await asyncio.to_thread(_load_session_transcript, session_id)
            ).strip()

            if not transcript_text:
                logger.warning(f"No transcript available for session {session_id}")
                return None
            
            # Check if transcript has changed significantly
            current_length = len(transcript_text)
            last_length = self.last_transcript_length.get(session_id, 0)
            
            # Only generate if we have new content (at least 100 chars difference)
            if current_length - last_length < 100:
                logger.debug(f"Not enough new content for session {session_id}")
                return None
            
            # Update tracking
            self.last_transcript_length[session_id] = current_length
            
            # Generate summary using LLM service
            summary_data = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "transcript_length": current_length,
                "summary": "Meeting in progress...",
                "key_points": [],
                "action_items": [],
                "topics": [],
                "sentiment": "neutral"
            }
            
            # Only generate AI summary if we have enough content
            if current_length > 200:
                try:
                    org_id = self.session_org_id.get(session_id)
                    if org_id is None:
                        # Resolve lazily in case it wasn't cached at start.
                        org_id = await asyncio.to_thread(_resolve_org_id, session_id)
                        self.session_org_id[session_id] = org_id

                    # Use 'fast' tier for live progressive updates.
                    ai_summary = await asyncio.to_thread(
                        _summarize_with_org, transcript_text, org_id, "fast"
                    )
                    if ai_summary:
                        summary_data.update(ai_summary)
                    else:
                        # Basic extraction without AI
                        sentences = [s.strip() for s in transcript_text.split('.') if len(s.strip()) > 20]
                        summary_data["key_points"] = sentences[:5] if sentences else []
                        summary_data["summary"] = f"Meeting ongoing with {len(sentences)} key points discussed."

                except Exception as e:
                    logger.error(f"Error generating AI summary: {e}")
                    # Continue with basic summary
            
            return summary_data
            
        except Exception as e:
            logger.error(f"Error generating summary for session {session_id}: {e}")
            return None
    
    def get_cached_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get the last cached summary for a session"""
        return self.summaries_cache.get(session_id)
    
    def is_running(self, session_id: str) -> bool:
        """Check if auto-summarization is running for a session"""
        return session_id in self.active_tasks
    
    def get_status(self, session_id: str) -> Dict[str, Any]:
        """Get status of auto-summarization for a session"""
        settings = SystemService.load_settings()
        
        return {
            "enabled": settings.enableLiveSummarization,
            "running": self.is_running(session_id),
            "interval": settings.summaryUpdateInterval,
            "last_summary_time": self.last_summary_time.get(session_id),
            "cached_summary": self.summaries_cache.get(session_id) is not None
        }
    
    async def force_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Force generate a summary immediately, regardless of interval"""
        summary = await self._generate_summary(session_id)
        if summary:
            self.summaries_cache[session_id] = summary
            self.last_summary_time[session_id] = time.time()
        return summary
    
    async def _get_current_word_count(self, session_id: str) -> int:
        """Get the current word count for a session's transcript"""
        try:
            # First try to get word count from WebSocket tracking (most accurate for live)
            try:
                from api.websocket_auto_summary import session_word_counts
                if session_id in session_word_counts:
                    word_count = session_word_counts[session_id]
                    self.total_word_count[session_id] = word_count
                    return word_count
            except ImportError:
                pass
            
            # Fallback to the canonical DB transcript_simple column.
            try:
                transcript_text = await asyncio.to_thread(
                    _load_session_transcript, session_id
                )
                if transcript_text:
                    total = len(transcript_text.split())
                    self.total_word_count[session_id] = total
                    return total
            except Exception:
                pass

            return 0

        except Exception as e:
            logger.debug(f"Could not get word count for session {session_id}: {e}")
            return 0
    
    async def generate_final_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Generate a comprehensive final summary when recording stops
        This is separate from the periodic summaries during recording
        """
        try:
            settings = SystemService.load_settings()
            
            if not settings.enableFinalSummary:
                logger.info(f"Final summary disabled for session {session_id}")
                return None
            
            # Get the full transcript from the canonical DB column. By the
            # time the recording stops, transcript_simple holds the complete
            # browser-STT or appliance-whisper output.
            full_transcript = (
                await asyncio.to_thread(_load_session_transcript, session_id)
            ).strip()

            if len(full_transcript) < 50:
                logger.info(f"Not enough content for final summary in session {session_id}")
                return None

            # Generate comprehensive final summary
            final_summary = {
                "type": "final",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_words": len(full_transcript.split()),
                "periodic_summaries_count": len(self.summary_history.get(session_id, [])),
                "summary": "Complete meeting summary...",
                "key_points": [],
                "action_items": [],
                "decisions": [],
                "topics": [],
                "participants": [],
                "sentiment": "neutral",
                "duration_seconds": 0
            }
            
            # Generate comprehensive AI summary for the full transcript using
            # the org-aware 'quality' tier provider.
            try:
                org_id = self.session_org_id.get(session_id)
                if org_id is None:
                    org_id = await asyncio.to_thread(_resolve_org_id, session_id)
                    self.session_org_id[session_id] = org_id

                ai_summary = await asyncio.to_thread(
                    _summarize_with_org, full_transcript, org_id, "quality"
                )

                if ai_summary:
                    final_summary.update(ai_summary)
                    final_summary["summary"] = f"[FINAL SUMMARY] {ai_summary.get('summary', 'Complete meeting transcript processed.')}"
                else:
                    # Basic extraction without AI
                    sentences = [s.strip() for s in full_transcript.split('.') if len(s.strip()) > 20]
                    final_summary["key_points"] = sentences[:10] if sentences else []
                    final_summary["summary"] = f"[FINAL SUMMARY] Meeting completed with {len(sentences)} discussion points."

            except Exception as e:
                logger.error(f"Error generating AI final summary: {e}")
                final_summary["summary"] = "[FINAL SUMMARY] Meeting completed. Full transcript available."
            
            # Store on the canonical RecordingSession row.  Do not report a
            # completed final summary to callers until it is durable; callers
            # use the returned payload to update sockets/UI while subsequent
            # reads use this database field.
            persisted = await asyncio.to_thread(
                _persist_final_summary, session_id, final_summary
            )
            if not persisted:
                return None
            
            # Clean up history for this session
            self.summary_history.pop(session_id, None)
            self.last_summary_word_count.pop(session_id, None)
            self.total_word_count.pop(session_id, None)
            
            logger.info(f"Generated final summary for session {session_id}")
            return final_summary
            
        except Exception as e:
            logger.error(f"Error generating final summary for session {session_id}: {e}")
            return None
    
    async def cleanup(self):
        """Clean up all active tasks"""
        tasks_to_cancel = list(self.active_tasks.keys())
        for session_id in tasks_to_cancel:
            await self.stop_auto_summarization(session_id)
        
        self.summaries_cache.clear()
        self.summary_history.clear()
        logger.info("Auto-summarization service cleaned up")


# Singleton instance
auto_summarization_service = AutoSummarizationService()
