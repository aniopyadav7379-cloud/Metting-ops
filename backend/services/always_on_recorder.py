"""
Always-On Recording Service
Continuous audio monitoring with automatic meeting segmentation.
When speech is detected, creates a new session and starts recording.
When silence exceeds threshold, finalizes the session.
"""

import asyncio
import logging
import os
import time
import numpy as np
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class AlwaysOnRecorder:
    """
    State machine:
    - IDLE: Listening for speech, not recording
    - RECORDING: Speech detected, actively recording a meeting
    - SILENCE_GAP: Speech stopped, counting silence before finalizing
    - CHECKING_CONTEXT: Silence threshold reached, waiting for post-gap speech
      to ask LLM whether it's the same meeting or a new one
    - FINALIZING: Wrapping up session, processing transcript
    """

    def __init__(self):
        self.state = "IDLE"
        self.is_enabled = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.device_id: Optional[str] = None

        # Owning context for sessions this recorder creates. RecordingSession
        # rows are NOT NULL on organization_id (database/models.py per
        # 004_multi_org_scoping), so always-on sessions MUST be scoped to the
        # user/org that enabled the mode. Set via attach_owner() by the API
        # layer right after the singleton is started. If both are still None at
        # insert time we skip session creation rather than insert an unowned row.
        self.organization_id: Optional[int] = None
        self.user_id: Optional[int] = None

        # Configuration
        self.silence_threshold_chunks = 20  # 20 * 15s = 5 min silence to split
        self.min_meeting_chunks = 2  # 2 * 15s = 30s minimum meeting
        self.chunk_duration = 15  # seconds per chunk
        self.sample_rate = 44100
        self.speech_threshold_db = -75.0
        # v3.18.0 — silence-gap auto-segmentation default OFF. When off, a long
        # quiet stretch never ends the session or spawns a new one; we just
        # reset the silence counter and keep recording until explicit Stop (or
        # the v3.17.1 watchdog catches a truly abandoned recording). Opt back
        # in per deploy by setting ALWAYS_ON_AUTO_SEGMENT=1 (env or compose).
        self._auto_segment_enabled = os.getenv(
            "ALWAYS_ON_AUTO_SEGMENT", "0"
        ).strip().lower() in ("1", "true", "yes", "on")

        # State tracking
        self.current_session_id: Optional[str] = None
        self.consecutive_silent = 0
        self.speech_chunks = 0
        self.meetings_created = 0

        # Context checking state (LLM-based meeting boundary detection)
        self.context_check_speech_chunks = 0  # Speech chunks received after gap
        self.context_check_max_wait_chunks = 8  # 8 * 15s = 2 min max wait for speech
        self.context_check_wait_chunks = 0  # Total chunks waited in CHECKING_CONTEXT
        self.context_check_speech_needed = 2  # Need 2 chunks of speech for LLM check
        self.pre_gap_transcript: Optional[str] = None  # Transcript before the gap
        self.gap_duration_minutes: float = 0.0  # Duration of the silence gap

    def attach_owner(self, organization_id: Optional[int], user_id: Optional[int]):
        """Bind the org/user that owns sessions this recorder creates.

        Called by the API layer immediately after enabling always-on mode so
        every auto-detected meeting is scoped to the requesting user's org.
        Without this, _start_new_meeting() refuses to insert a session
        (organization_id is NOT NULL) and logs a warning instead of creating
        an orphaned, unscoped row."""
        self.organization_id = organization_id
        self.user_id = user_id
        logger.info(
            "Always-on owner attached: organization_id=%s user_id=%s",
            organization_id,
            user_id,
        )

    async def start(self, device_id: Optional[str] = None):
        """Enable always-on mode"""
        if self.is_enabled:
            return {"status": "already_running"}

        self.is_enabled = True
        self.state = "IDLE"
        self.device_id = device_id
        self.meetings_created = 0

        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Always-on recording mode ENABLED")
        return {"status": "started"}

    async def stop(self):
        """Disable always-on mode"""
        self.is_enabled = False

        # If currently recording, stop gracefully
        if self.state in ("RECORDING", "SILENCE_GAP", "CHECKING_CONTEXT") and self.current_session_id:
            await self._finalize_meeting()

        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        self.state = "IDLE"
        logger.info("Always-on recording mode DISABLED")
        return {"status": "stopped", "meetings_created": self.meetings_created}

    def get_status(self):
        """Get current always-on status"""
        return {
            "enabled": self.is_enabled,
            "state": self.state,
            "current_session_id": self.current_session_id,
            "consecutive_silent_chunks": self.consecutive_silent,
            "speech_chunks": self.speech_chunks,
            "meetings_created": self.meetings_created,
            "silence_threshold_minutes": (self.silence_threshold_chunks * self.chunk_duration) / 60,
            "auto_segment_enabled": self._auto_segment_enabled,
            "device_id": self.device_id,
        }

    async def _monitor_loop(self):
        """Main monitoring loop - captures audio and detects speech"""
        from services.working_audio_service import audio_service

        # Find audio source
        source = self.device_id or audio_service._find_pulse_source() or "default"

        logger.info(f"Always-on monitoring started, source: {source}")

        chunk_samples = self.chunk_duration * self.sample_rate

        # Start continuous ffmpeg capture to stdout as raw PCM
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "pulse",
            "-i",
            source,
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "-f",
            "s16le",  # Raw 16-bit PCM to stdout
            "-",
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            bytes_per_chunk = chunk_samples * 2  # 16-bit = 2 bytes per sample

            while self.is_enabled:
                # Read one chunk of audio
                raw_data = await process.stdout.read(bytes_per_chunk)
                if not raw_data:
                    logger.warning("Audio stream ended unexpectedly")
                    break

                # Convert to numpy
                audio = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0

                # Check for speech
                has_speech = self._chunk_has_speech(audio)

                if self.state == "IDLE":
                    if has_speech:
                        logger.info("Speech detected - starting new meeting")
                        await self._start_new_meeting()
                        self.state = "RECORDING"
                        self.speech_chunks = 1
                        self.consecutive_silent = 0

                elif self.state == "RECORDING":
                    if has_speech:
                        self.speech_chunks += 1
                        self.consecutive_silent = 0
                    else:
                        self.consecutive_silent += 1
                        if self.consecutive_silent >= self.silence_threshold_chunks:
                            if not self._auto_segment_enabled:
                                # v3.18.0 default-off path: long silence does
                                # NOT end the session or spawn a new one. Just
                                # reset the counter, log it, and keep recording
                                # until explicit Stop. The watchdog (v3.17.1)
                                # handles truly abandoned sessions.
                                gap_min = (self.consecutive_silent * self.chunk_duration) / 60.0
                                logger.info(
                                    "Silence threshold reached (%.1f min) but "
                                    "ALWAYS_ON_AUTO_SEGMENT=off — continuing the "
                                    "same session",
                                    gap_min,
                                )
                                self.consecutive_silent = 0
                            elif self.speech_chunks < self.min_meeting_chunks:
                                logger.info(
                                    f"Meeting too short ({self.speech_chunks} chunks), discarding"
                                )
                                await self._discard_meeting()
                                self.state = "IDLE"
                                self.speech_chunks = 0
                                self.consecutive_silent = 0
                            else:
                                # Transition to CHECKING_CONTEXT instead of immediate finalize
                                self.gap_duration_minutes = (
                                    self.consecutive_silent * self.chunk_duration
                                ) / 60.0
                                self.pre_gap_transcript = self._get_transcript_text(
                                    last_n_words=200
                                )
                                self.context_check_speech_chunks = 0
                                self.context_check_wait_chunks = 0
                                self.state = "CHECKING_CONTEXT"
                                logger.info(
                                    f"Silence threshold reached ({self.consecutive_silent} chunks, "
                                    f"{self.gap_duration_minutes:.1f} min) - entering CHECKING_CONTEXT, "
                                    f"waiting for post-gap speech to query LLM"
                                )

                elif self.state == "CHECKING_CONTEXT":
                    self.context_check_wait_chunks += 1

                    if has_speech:
                        self.context_check_speech_chunks += 1
                        self.speech_chunks += 1
                        logger.info(
                            f"CHECKING_CONTEXT: post-gap speech chunk "
                            f"{self.context_check_speech_chunks}/"
                            f"{self.context_check_speech_needed}"
                        )

                        if self.context_check_speech_chunks >= self.context_check_speech_needed:
                            # We have enough post-gap speech, ask the LLM
                            post_gap_text = self._get_transcript_text(
                                last_n_words=200
                            )
                            is_same = await self._check_context_continuity(
                                self.pre_gap_transcript,
                                post_gap_text,
                                self.gap_duration_minutes,
                            )

                            if is_same:
                                # LLM says same meeting - reset silence and keep recording
                                logger.info(
                                    "LLM context check: SAME meeting - "
                                    "merging, continuing recording"
                                )
                                self.consecutive_silent = 0
                                self.state = "RECORDING"
                            else:
                                # LLM says new meeting - finalize old, start new
                                logger.info(
                                    "LLM context check: NEW meeting - "
                                    "finalizing previous meeting and starting new one"
                                )
                                await self._finalize_meeting()
                                # Start a new meeting for the post-gap speech
                                await self._start_new_meeting()
                                self.state = "RECORDING"
                                self.speech_chunks = self.context_check_speech_chunks
                                self.consecutive_silent = 0

                    else:
                        # Still silence during context check
                        if self.context_check_wait_chunks >= self.context_check_max_wait_chunks:
                            # Waited too long with no speech - meeting truly ended
                            logger.info(
                                f"CHECKING_CONTEXT: no speech after "
                                f"{self.context_check_wait_chunks} chunks "
                                f"({self.context_check_wait_chunks * self.chunk_duration}s) "
                                f"- finalizing meeting (silence timeout)"
                            )
                            await self._finalize_meeting()
                            self.state = "IDLE"
                            self.speech_chunks = 0
                            self.consecutive_silent = 0

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Always-on monitor error: {e}", exc_info=True)
        finally:
            process.terminate()
            try:
                await process.wait()
            except Exception:
                pass
            logger.info("Always-on monitor loop ended")

    def _chunk_has_speech(self, audio: np.ndarray) -> bool:
        """Check if audio chunk has speech (reuse existing logic)"""
        if len(audio) == 0:
            return False
        rms = np.sqrt(np.mean(audio**2))
        if rms < 1e-10:
            return False
        db = 20 * np.log10(rms)
        return db > self.speech_threshold_db

    def _get_transcript_text(self, last_n_words: int = 200) -> str:
        """Extract the last N words from the live transcription buffer.

        The transcript buffer lives on the live_recording_transcription singleton
        and contains dicts with a 'text' key for each transcribed segment.
        """
        try:
            from services.live_recording_transcription import (
                live_recording_transcription,
            )

            buffer = live_recording_transcription.transcript_buffer
            if not buffer:
                return ""

            # Collect text from segments in reverse until we have enough words
            words: list[str] = []
            for segment in reversed(buffer):
                text = segment.get("text", "").strip()
                if text:
                    segment_words = text.split()
                    words = segment_words + words
                    if len(words) >= last_n_words:
                        break

            # Trim to the requested number of words (keep the last N)
            if len(words) > last_n_words:
                words = words[-last_n_words:]

            return " ".join(words)
        except Exception as e:
            logger.error(f"Error extracting transcript text: {e}")
            return ""

    def _resolve_org_id_for_current_session(self) -> Optional[int]:
        """Resolve the active org id for the current always-on session, falling
        back to the default org slug ('magic-unicorn') when the session row has
        no organization scoping."""
        try:
            from database.database import SessionLocal
            from database.models import RecordingSession
            from auth.models import Organization
            from auth.organization import DEFAULT_ORG_SLUG

            db = SessionLocal()
            try:
                if self.current_session_id:
                    row = (
                        db.query(RecordingSession.organization_id)
                        .filter(RecordingSession.session_id == self.current_session_id)
                        .first()
                    )
                    if row and row[0] is not None:
                        return int(row[0])

                default = (
                    db.query(Organization.id)
                    .filter(Organization.slug == DEFAULT_ORG_SLUG)
                    .first()
                )
                if default and default[0] is not None:
                    return int(default[0])
            finally:
                db.close()
        except Exception as exc:
            logger.debug(f"Always-on org resolution failed: {exc}")
        return None

    def _registry_chat_sync(
        self,
        org_id: int,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        """Run a synchronous chat_sync via the org-aware ProviderRegistry."""
        try:
            from database.database import SessionLocal
            from services.providers import get_provider_registry

            db = SessionLocal()
            try:
                registry = get_provider_registry(db)
                # 'fast' tier — context-continuity is a low-latency yes/no.
                provider = registry.get_llm(org_id=org_id, task="fast")
            finally:
                db.close()

            return provider.chat_sync(
                system_prompt,
                user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            logger.warning(f"Always-on registry LLM call failed: {exc}")
            return None

    async def _check_context_continuity(
        self,
        before_text: str,
        after_text: str,
        gap_duration_minutes: float,
    ) -> bool:
        """Ask the LLM whether speech before and after a silence gap belongs
        to the same meeting or a new one.

        Returns True if the LLM determines it is the SAME meeting (merge),
        False if it is a NEW meeting or the LLM is unavailable (finalize).
        """
        # If we don't have enough text on either side, fall back to simple behavior
        if not before_text or not after_text:
            logger.info(
                "Context check: insufficient transcript text on one side of gap, "
                "falling back to finalize"
            )
            return False

        try:
            system_prompt = (
                "You analyze meeting transcripts. Given text spoken before and "
                "after a silence gap, determine if this is the SAME meeting "
                "resuming or a NEW different meeting starting. Consider topic "
                "continuity, speaker context, and conversational flow. Respond "
                "with exactly one word: SAME or NEW"
            )

            user_prompt = (
                f"BEFORE SILENCE GAP:\n{before_text}\n\n"
                f"[{gap_duration_minutes:.1f} minute silence gap]\n\n"
                f"AFTER SILENCE GAP:\n{after_text}"
            )

            loop = asyncio.get_event_loop()

            # 1. Try the org-aware ProviderRegistry first.
            response: Optional[str] = None
            org_id = await loop.run_in_executor(
                None, self._resolve_org_id_for_current_session
            )

            if org_id is not None:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._registry_chat_sync(
                        org_id, system_prompt, user_prompt, max_tokens=10, temperature=0.1
                    ),
                )

            if not response:
                logger.warning(
                    "Context check: registry-routed LLM call returned empty, falling back to finalize"
                )
                return False

            # Parse the response - look for SAME or NEW
            decision = response.strip().upper().split()[0] if response.strip() else ""
            # Strip punctuation from the decision word
            decision = decision.strip(".,!?;:")

            if decision == "SAME":
                logger.info(
                    f"Context check LLM decision: SAME (raw response: '{response.strip()}')"
                )
                return True
            elif decision == "NEW":
                logger.info(
                    f"Context check LLM decision: NEW (raw response: '{response.strip()}')"
                )
                return False
            else:
                logger.warning(
                    f"Context check: unclear LLM response '{response.strip()}', "
                    "defaulting to NEW (finalize)"
                )
                return False

        except Exception as e:
            logger.error(
                f"Context check: LLM call failed ({e}), falling back to finalize",
                exc_info=True,
            )
            return False

    async def _start_new_meeting(self):
        """Create a new session and start recording"""
        import uuid
        from services.working_audio_service import audio_service

        session_id = str(uuid.uuid4())

        # Refuse to create an unowned session. organization_id is NOT NULL on
        # Postgres (004_multi_org_scoping); a None owner would either be
        # rejected by the DB or, on the weaker SQLite test schema, produce an
        # orphaned row no org can ever see. Skip + warn instead.
        if self.organization_id is None and self.user_id is None:
            logger.warning(
                "Always-on _start_new_meeting called with no owner attached "
                "(organization_id and user_id both None) - skipping session "
                "creation. Call attach_owner() after start() to record sessions."
            )
            self.state = "IDLE"
            return

        # Create session in database
        try:
            from database.database import SessionLocal
            from database.models import RecordingSession

            db = SessionLocal()
            try:
                new_session = RecordingSession(
                    session_id=session_id,
                    name=f"Auto-recorded meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
                    description="Automatically detected by always-on recording",
                    status="recording",
                    created_at=datetime.now(timezone.utc),
                    started_at=datetime.now(timezone.utc),
                    organization_id=self.organization_id,
                    user_id=self.user_id,
                )
                db.add(new_session)
                db.commit()
                db.refresh(new_session)
            finally:
                db.close()

            # Start ffmpeg recording
            success, file_path = audio_service.start_recording(
                session_id, device_id=self.device_id
            )
            if success:
                # Normalize path to prevent double-nesting
                file_path = os.path.normpath(os.path.abspath(file_path))
                self.current_session_id = session_id
                logger.info(
                    f"Auto-started recording: session={session_id}, file={file_path}"
                )

                # Start live transcription
                from services.live_recording_transcription import (
                    live_recording_transcription,
                )

                await live_recording_transcription.start_monitoring(
                    file_path, session_id
                )
            else:
                logger.error(f"Failed to start auto-recording: {file_path}")
                self.state = "IDLE"

        except Exception as e:
            logger.error(f"Error starting auto-meeting: {e}", exc_info=True)
            self.state = "IDLE"

    async def _finalize_meeting(self):
        """Stop recording and process the meeting"""
        if not self.current_session_id:
            return

        session_id = self.current_session_id
        logger.info(f"Finalizing auto-recorded meeting: {session_id}")

        try:
            # Stop live transcription
            from services.live_recording_transcription import (
                live_recording_transcription,
            )

            await live_recording_transcription.stop_monitoring()

            # Stop ffmpeg recording
            from services.working_audio_service import audio_service

            success, result = audio_service.stop_recording(session_id)

            if success:
                # Normalize path to prevent double-nesting
                if result:
                    result = os.path.normpath(os.path.abspath(result))

                # Update session in DB
                from database.database import SessionLocal
                from database.models import RecordingSession

                db = SessionLocal()
                try:
                    session = (
                        db.query(RecordingSession)
                        .filter(RecordingSession.session_id == session_id)
                        .first()
                    )
                    if session:
                        session.status = "processing"
                        session.ended_at = datetime.now(timezone.utc)
                        session.audio_file = result
                        if session.started_at:
                            started = session.started_at
                            if started.tzinfo is None:
                                started = started.replace(tzinfo=timezone.utc)
                            session.duration = (
                                datetime.now(timezone.utc) - started
                            ).total_seconds()
                        db.commit()

                        # Trigger post-processing (transcription + AI summary)
                        try:
                            from api.simple_recording_db import process_recording

                            asyncio.create_task(
                                process_recording(
                                    session_id=session.session_id,
                                    audio_file=result,
                                    db_session_id=session.id,
                                )
                            )
                            logger.info(
                                f"Post-processing scheduled for {session_id}"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to schedule post-processing: {e}"
                            )
                finally:
                    db.close()

                self.meetings_created += 1
                logger.info(
                    f"Meeting finalized: {session_id} (total: {self.meetings_created})"
                )
            else:
                logger.error(f"Failed to stop recording: {result}")

        except Exception as e:
            logger.error(f"Error finalizing meeting: {e}", exc_info=True)
        finally:
            self.current_session_id = None

    async def _discard_meeting(self):
        """Discard a too-short meeting"""
        if not self.current_session_id:
            return

        session_id = self.current_session_id
        logger.info(f"Discarding short auto-recording: {session_id}")

        try:
            from services.live_recording_transcription import (
                live_recording_transcription,
            )

            await live_recording_transcription.stop_monitoring()

            from services.working_audio_service import audio_service

            audio_service.stop_recording(session_id)

            # Delete the session from DB
            from database.database import SessionLocal
            from database.models import RecordingSession

            db = SessionLocal()
            try:
                session = (
                    db.query(RecordingSession)
                    .filter(RecordingSession.session_id == session_id)
                    .first()
                )
                if session:
                    # Delete the audio file if it exists
                    if session.audio_file and os.path.exists(session.audio_file):
                        os.unlink(session.audio_file)
                    db.delete(session)
                    db.commit()
            finally:
                db.close()

        except Exception as e:
            logger.error(f"Error discarding meeting: {e}", exc_info=True)
        finally:
            self.current_session_id = None


# Global singleton
always_on_recorder = AlwaysOnRecorder()
