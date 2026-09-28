"""
Unified Agent Service

Routes all LLM calls through the org-aware ProviderRegistry. The service tracks
the resolved organization_id per session so each call honors the org's
configured LLM provider — no silent fallback to a default provider.
"""
import asyncio
import json
import logging
import re
import time
from typing import Dict, Optional, List
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from database.database import get_db_session, SessionLocal
from database.models import RecordingSession
from models.unified_agent import UnifiedMeetingAgent
from services.progressive_interval_manager import progressive_interval_manager, ProgressiveConfig
from services.providers import get_provider_registry

logger = logging.getLogger(__name__)


def _resolve_org_id_for_session(session_id: str) -> Optional[int]:
    """Look up organization_id for a session_id (uuid or int) from the DB."""
    db = SessionLocal()
    try:
        row = (
            db.query(RecordingSession.organization_id)
            .filter(RecordingSession.session_id == session_id)
            .first()
        )
        if row and row[0] is not None:
            return int(row[0])
        # Fallback to integer id
        try:
            row = (
                db.query(RecordingSession.organization_id)
                .filter(RecordingSession.id == int(session_id))
                .first()
            )
            if row and row[0] is not None:
                return int(row[0])
        except (ValueError, TypeError):
            pass
    except Exception as exc:
        logger.debug(f"Could not resolve org_id for session {session_id}: {exc}")
    finally:
        db.close()
    return None


class UnifiedAgentService:
    """Progressive meeting agent that delegates LLM calls to ProviderRegistry."""

    def __init__(self):
        self.active_sessions: Dict[str, Dict] = {}
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        logger.info("Unified Agent Service initialized (ProviderRegistry-routed)")

    @property
    def model_name(self) -> str:
        """Return the configured task=quality model name from environment.

        Used only for log/display purposes; actual model selection happens
        per-org inside ProviderRegistry.get_llm()."""
        import os
        return os.getenv("LLM_MODEL_QUALITY", "configured-by-registry")

    def _extract_json_from_ai_response(self, response_text: str) -> Optional[str]:
        """
        Extract JSON from AI response that may contain extra text
        Handles markdown code blocks, prefix text, and trailing content
        """
        if not response_text:
            return None

        # Strip leading/trailing whitespace first
        cleaned = response_text.strip()

        # Remove common prefix patterns (with and without leading spaces)
        prefix_patterns = [
            "Response:", " Response:", "Here is the analysis:", "Here's the analysis:",
            "Output:", " Output:", "JSON:", " JSON:", "Result:", " Result:",
            "Analysis:", " Analysis:", "Summary:", " Summary:", "Here is the JSON:",
            "Here's the JSON:", "Returning JSON:", "Generated JSON:", "Answer:",
            "Based on the transcript:", "After analyzing:", "The meeting summary:",
            "Here is the summary:", "Here's the summary:", "Meeting analysis:",
            "Solution 1:", "Solution:", "<|end|>", " <|end|>",
        ]

        for pattern in prefix_patterns:
            if pattern.lower() in cleaned.lower():
                idx = cleaned.lower().find(pattern.lower())
                cleaned = cleaned[idx + len(pattern):].strip()
                break

        # Handle markdown code blocks
        if '```json' in cleaned:
            start = cleaned.find('```json') + 7
            end = cleaned.find('```', start)
            if end > start:
                return cleaned[start:end].strip()
        elif '```' in cleaned:
            start = cleaned.find('```') + 3
            end = cleaned.find('```', start)
            if end > start:
                potential = cleaned[start:end].strip()
                if potential.startswith(('json', 'JSON')):
                    potential = potential[4:].strip()
                if potential.startswith('{'):
                    return potential

        # Extract raw JSON using brace matching
        json_start = cleaned.find('{')
        if json_start >= 0:
            brace_count = 0
            for i in range(json_start, len(cleaned)):
                if cleaned[i] == '{':
                    brace_count += 1
                elif cleaned[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_candidate = cleaned[json_start:i+1]
                        if '"executive"' in json_candidate or '"bullets"' in json_candidate:
                            return json_candidate

            # If we couldn't find a complete JSON object, try to find just the opening
            remaining = cleaned[json_start:]
            if remaining.count('{') > remaining.count('}'):
                for end_marker in ['\n\n', 'Solution', '<|end|>', 'QUESTION:', 'ANSWER:']:
                    if end_marker in remaining:
                        partial = remaining[:remaining.find(end_marker)]
                        open_braces = partial.count('{')
                        close_braces = partial.count('}')
                        if open_braces > close_braces:
                            partial += '}' * (open_braces - close_braces)
                        return partial

        return None

    async def _call_llm(
        self,
        session_id: str,
        prompt: str,
        max_tokens: int = 500,
        temperature: float = 0.7,
        system_prompt: str = "You are a meeting assistant.",
        timeout: int = 30,
        task: str = "quality",
    ) -> Optional[str]:
        """Resolve the session's org-aware LLM provider and run a chat call.

        Returns the response text, or None on any failure. Silent fallback to
        a default provider is intentionally not done — it would bypass per-org
        provider configuration and billing.
        """
        org_id = self.active_sessions.get(session_id, {}).get("organization_id")
        if org_id is None:
            org_id = await asyncio.to_thread(_resolve_org_id_for_session, session_id)
            if session_id in self.active_sessions:
                self.active_sessions[session_id]["organization_id"] = org_id

        if org_id is None:
            logger.error(
                f"unified_agent: no org_id resolvable for session {session_id}; cannot route LLM call"
            )
            return None

        def _do_call() -> Optional[str]:
            db = SessionLocal()
            try:
                registry = get_provider_registry(db)
                provider = registry.get_llm(org_id=org_id, task=task)
            except Exception as exc:
                logger.error(
                    f"unified_agent: registry resolution failed for org {org_id} task={task}: {exc}",
                    exc_info=True,
                )
                return None
            finally:
                db.close()

            try:
                start = time.time()
                text = provider.chat_sync(
                    system_prompt,
                    prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                elapsed = time.time() - start
                if text:
                    tokens = len(text.split())
                    rate = tokens / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"unified_agent LLM call ({getattr(provider, 'model', '?')}) "
                        f"{elapsed:.1f}s ({rate:.1f} tok/s)"
                    )
                return text.strip() if text else None
            except Exception as exc:
                logger.error(f"unified_agent: provider chat_sync failed: {exc}", exc_info=True)
                return None

        try:
            return await asyncio.wait_for(asyncio.to_thread(_do_call), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"unified_agent: LLM call timed out after {timeout}s (session {session_id})")
            return None

    async def start_meeting_analysis(self, session_id: str, websocket_callback=None) -> bool:
        """Start progressive meeting analysis for a session"""
        if session_id in self.active_sessions:
            logger.warning(f"Meeting analysis already active for session {session_id}")
            return False

        try:
            db_session = get_db_session()

            # Get or create default agent
            agent = UnifiedMeetingAgent.get_default_agent(db_session)
            if not agent:
                agent = UnifiedMeetingAgent.create_default_agent(db_session)

            # Parse progressive config
            config = ProgressiveConfig.from_dict(agent.progressive_config)

            # Initialize progressive intervals
            progressive_interval_manager.initialize_session(session_id, str(agent.id), config)

            # Resolve org_id once so subsequent _call_llm hits don't have to
            # re-query the DB on every interval tick.
            org_id = await asyncio.to_thread(_resolve_org_id_for_session, session_id)

            # Store session info
            session_info = {
                'session_id': session_id,
                'agent': agent,
                'config': config,
                'websocket_callback': websocket_callback,
                'started_at': datetime.now(timezone.utc),
                'summary_count': 0,
                'organization_id': org_id,
            }

            self.active_sessions[session_id] = session_info

            # Start monitoring task
            task = asyncio.create_task(self._monitoring_loop(session_id))
            self.monitoring_tasks[session_id] = task

            logger.info(f"Started meeting analysis for session {session_id}")
            logger.info(f"  Agent: {agent.name}")
            logger.info(f"  Model: {self.model_name}")
            logger.info(f"  Initial interval: {config.initial_word_count} words")

            return True

        except Exception as e:
            logger.error(f"Failed to start meeting analysis: {e}")
            return False
        finally:
            if db_session:
                db_session.close()

    async def stop_meeting_analysis(self, session_id: str) -> Optional[Dict]:
        """Stop meeting analysis and generate final summary"""
        if session_id not in self.active_sessions:
            return None

        try:
            session_info = self.active_sessions[session_id]

            # Cancel monitoring task
            if session_id in self.monitoring_tasks:
                task = self.monitoring_tasks[session_id]
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                del self.monitoring_tasks[session_id]

            # Generate final comprehensive summary
            final_summary = await self._generate_final_summary(session_id)

            # Generate meeting title
            meeting_title = await self._generate_meeting_title(session_id)

            # Clean up
            del self.active_sessions[session_id]
            progressive_interval_manager.cleanup_session(session_id)

            logger.info(f"Stopped meeting analysis for session {session_id}")
            logger.info(f"  Generated {session_info.get('summary_count', 0)} progressive summaries")
            if meeting_title:
                logger.info(f"  Suggested title: {meeting_title}")

            return {
                "final_summary": final_summary,
                "meeting_title": meeting_title,
                "summary_count": session_info.get('summary_count', 0)
            }

        except Exception as e:
            logger.error(f"Error stopping meeting analysis: {e}")
            return None

    async def _monitoring_loop(self, session_id: str):
        """Main monitoring loop for progressive analysis"""
        session_info = self.active_sessions[session_id]
        agent = session_info['agent']
        config = session_info['config']

        try:
            while session_id in self.active_sessions:
                # Get current word count
                current_word_count = await self._get_session_word_count(session_id)

                # Check if we should generate a summary
                should_generate, interval_info = progressive_interval_manager.should_generate_summary(
                    session_id, str(agent.id), current_word_count, config
                )

                if should_generate:
                    await self._generate_progressive_summary(session_id, interval_info)

                # Check every 5 seconds
                await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for session {session_id}")
            raise
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")

    async def _get_session_word_count(self, session_id: str) -> int:
        """Get current word count for a session"""
        try:
            # Try WebSocket registry first
            try:
                from api.websocket_auto_summary import session_word_counts
                if session_id in session_word_counts:
                    return session_word_counts[session_id]
            except ImportError:
                pass

            # Fallback to live transcription buffer
            try:
                from services.live_recording_transcription import live_recording_transcription
                if hasattr(live_recording_transcription, 'transcript_buffer') and live_recording_transcription.transcript_buffer:
                    if live_recording_transcription.session_id == session_id:
                        total = sum(len(seg.get('text', '').split()) for seg in live_recording_transcription.transcript_buffer)
                        return total
            except Exception:
                pass

            return 0

        except Exception as e:
            logger.debug(f"Could not get word count for session {session_id}: {e}")
            return 0

    async def _generate_progressive_summary(self, session_id: str, interval_info: Dict):
        """Generate progressive plain text summary using Granite 3.3 2B"""
        session_info = self.active_sessions[session_id]
        agent = session_info['agent']

        start_time = time.time()

        try:
            # Get transcript
            transcript = await self._get_session_transcript(session_id)

            if not transcript or len(transcript.split()) < 10:
                logger.debug(f"Not enough content for summary (session {session_id})")
                return

            # Generate plain text summary
            summary_text = await self._generate_text_summary(session_id, transcript, interval_info)

            if not summary_text:
                logger.warning(f"Failed to generate progressive summary for session {session_id}")
                return

            processing_time = int((time.time() - start_time) * 1000)
            session_info['summary_count'] += 1

            # Save progressive summary to database (as plain text)
            await self._save_progressive_text_summary_to_db(
                session_id, interval_info, summary_text, processing_time
            )

            # Send via WebSocket - always broadcast for database-backed sessions
            websocket_callback = session_info.get('websocket_callback')
            if websocket_callback:
                await self._send_text_summary_update(session_id, session_info, interval_info, summary_text, processing_time)
            else:
                # Direct broadcast for database-backed sessions
                summary_message = {
                    "type": "progressive_summary",
                    "session_id": session_id,
                    "agent": {
                        "id": str(session_info['agent'].id),
                        "name": session_info['agent'].name,
                        "role": "unified"
                    },
                    "summary_data": {
                        "text": summary_text,
                        "word_count": interval_info['current_word_count'],
                        "interval_used": interval_info['current_interval'],
                        "next_interval": interval_info.get('next_interval', interval_info['current_interval']),
                        "model": self.model_name,
                        "processing_time_ms": processing_time
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }

                # Try to broadcast to /ws/transcription-auto/{session_id} connections
                from api.websocket_auto_summary import broadcast_progressive_summary
                await broadcast_progressive_summary(session_id, summary_message)

                # Also broadcast to /ws/transcription/{session_id} connections
                try:
                    from api.websocket_transcription import manager as transcription_manager
                    await transcription_manager.broadcast_transcription(session_id, summary_message)
                    logger.info(f"Broadcast progressive summary to transcription WebSocket for {session_id}")
                except Exception as e:
                    logger.debug(f"Could not broadcast to transcription WebSocket: {e}")

            logger.info(f"Generated progressive summary for session {session_id}")
            logger.info(f"  Word count: {interval_info['current_word_count']}")
            logger.info(f"  Interval: {interval_info['current_interval']} words")
            logger.info(f"  Processing time: {processing_time}ms")

        except Exception as e:
            logger.error(f"Error generating progressive summary: {e}")

    async def _generate_text_summary(self, session_id: str, transcript: str, interval_info: Dict) -> Optional[str]:
        """Generate a plain text summary for progressive updates"""
        # Truncate transcript if too long for real-time processing
        if len(transcript) > 6000:
            transcript = transcript[:6000] + "..."

        prompt = f"""Below is a meeting transcript. Write a 2-3 sentence summary of the key points.

Meeting transcript:
{transcript}

Brief summary:"""

        result = await self._call_llm(
            session_id=session_id,
            prompt=prompt,
            max_tokens=150,
            temperature=0.5,
            system_prompt="You are a fast meeting summarizer. Provide brief, clear summaries.",
            timeout=30,
            task="fast",
        )

        if not result:
            return None

        # Clean up the response
        cleaned_result = result.strip()

        if cleaned_result.lower().startswith("summary:"):
            cleaned_result = cleaned_result[8:].strip()

        # Remove common instruction artifacts from small models
        if "[Your answer should be" in cleaned_result:
            parts = cleaned_result.split("]")
            if len(parts) > 1:
                cleaned_result = parts[1].strip()

        if "**Final Answer**" in cleaned_result or "Final Answer:" in cleaned_result:
            cleaned_result = cleaned_result.split("**Final Answer**")[0].strip()
            cleaned_result = cleaned_result.split("Final Answer:")[0].strip()

        cleaned_result = cleaned_result.replace("**", "")

        # Limit length for real-time display
        if len(cleaned_result) > 300:
            sentences = cleaned_result.split('. ')
            cleaned_result = '. '.join(sentences[:3])
            if not cleaned_result.endswith('.'):
                cleaned_result += '.'

        if cleaned_result:
            logger.info(f"Generated progressive text summary: {cleaned_result[:80]}...")
            return cleaned_result

        return None

    async def _get_progressive_summaries(self, session_id: str) -> List[Dict]:
        """Get progressive summaries for a session to provide context for final summary"""
        try:
            from database.database import get_db_session
            from database.models import RecordingSession

            db = get_db_session()
            session = db.query(RecordingSession).filter(
                RecordingSession.session_id == session_id
            ).first()

            if session and session.progressive_summaries:
                return session.progressive_summaries
            else:
                return []

        except Exception as e:
            logger.error(f"Error getting progressive summaries: {e}")
            return []
        finally:
            if db:
                db.close()

    async def _get_session_transcript(self, session_id: str) -> str:
        """Get current transcript for a session"""
        try:
            # First try to get from live recording transcription buffer
            from services.live_recording_transcription import live_recording_transcription
            if hasattr(live_recording_transcription, 'transcript_buffer') and live_recording_transcription.transcript_buffer:
                if live_recording_transcription.session_id == session_id:
                    transcript_text = ' '.join(
                        segment.get('text', '') for segment in live_recording_transcription.transcript_buffer
                    )
                    if transcript_text:
                        logger.debug(f"Got transcript from live buffer: {len(transcript_text.split())} words")
                        return transcript_text

            # Try from database
            from database.database import get_db_session
            from database.models import RecordingSession
            db = get_db_session()
            try:
                session = db.query(RecordingSession).filter_by(session_id=session_id).first()
                if session and session.transcript:
                    return session.transcript
            finally:
                db.close()

            return ""

        except Exception as e:
            logger.error(f"Error getting transcript for session {session_id}: {e}")
            return ""

    async def _generate_final_summary(self, session_id: str) -> Optional[Dict]:
        """Generate comprehensive final summary using Granite 3.3 2B"""
        session_info = self.active_sessions.get(session_id)
        if not session_info:
            return None

        transcript = await self._get_session_transcript(session_id)

        if not transcript:
            return None

        # Get progressive summaries to provide additional context
        progressive_summaries = await self._get_progressive_summaries(session_id)
        progressive_context = ""
        if progressive_summaries:
            progressive_context = "\n\nPROGRESSIVE SUMMARIES DURING MEETING:\n"
            for i, summary in enumerate(progressive_summaries, 1):
                progressive_context += f"{i}. {summary.get('text', '')}\n"

        final_prompt = f"""TRANSCRIPT:
{transcript}
{progressive_context}

Provide JSON analysis:

{{
    "executive": "2-3 sentence summary",
    "bullets": ["key point 1", "key point 2"],
    "actions": [{{"action": "task", "owner": "person", "priority": "high"}}],
    "decisions": ["decision 1"],
    "title": "Meeting Title"
}}"""

        try:
            result = await self._call_llm(
                session_id=session_id,
                prompt=final_prompt,
                max_tokens=600,
                temperature=0.3,
                system_prompt="You are a meeting analyst. Provide comprehensive meeting analysis in valid JSON format.",
                timeout=120,
                task="quality",
            )

            if result:
                logger.info(f"Final summary generated using {self.model_name}")
                try:
                    json_str = self._extract_json_from_ai_response(result)
                    if json_str:
                        parsed_result = json.loads(json_str)
                        logger.info(f"Comprehensive final summary generated with {len(parsed_result.get('bullets', []))} key points")
                        return parsed_result
                    else:
                        raise json.JSONDecodeError("No JSON found", result, 0)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse final summary JSON, using fallback format")
                    return {
                        "executive": "Final summary generation completed. Please review the transcript for details.",
                        "bullets": [
                            "Meeting concluded successfully",
                            f"Total transcript length: {len(transcript.split())} words",
                            "Summary processing complete"
                        ],
                        "actions": [],
                        "decisions": [],
                        "title": f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    }

        except Exception as e:
            logger.error(f"Error generating final summary: {e}")

        return None

    async def _generate_meeting_title(self, session_id: str) -> Optional[str]:
        """Generate meeting title from transcript"""
        transcript = await self._get_session_transcript(session_id)
        if not transcript:
            return None

        title_prompt = f"""Based on this meeting transcript, generate a concise, descriptive meeting title (4-8 words):

{transcript[:4000]}...

Title should capture the main purpose/topic. Examples:
- "Q4 Budget Planning Review"
- "Product Launch Strategy Meeting"
- "Weekly Team Standup"

RESPOND WITH ONLY THE TITLE:"""

        try:
            result = await self._call_llm(
                session_id=session_id,
                prompt=title_prompt,
                max_tokens=30,
                temperature=0.3,
                system_prompt="You generate concise meeting titles. Respond with ONLY the title.",
                timeout=30,
                task="fast",
            )

            if result:
                title = result.replace('"', '').replace('\n', ' ').strip()
                return title[:100] if title else None

        except Exception as e:
            logger.error(f"Error generating meeting title: {e}")

        return None

    async def analyze_meeting(self, session_id: str, transcript: str) -> Dict:
        """
        Analyze a meeting transcript - with timeout protection
        Consolidates both analysis paths into one method
        """
        try:
            # Truncate very long transcripts to avoid timeout
            if len(transcript) > 20000:
                transcript = transcript[:20000] + "... [truncated for processing]"

            db_session = get_db_session()

            # Get or create default agent
            agent = UnifiedMeetingAgent.get_default_agent(db_session)
            if not agent:
                agent = UnifiedMeetingAgent.create_default_agent(db_session)

            # Simplified prompt
            prompt = f"""Analyze this meeting transcript and provide a brief summary:

TRANSCRIPT:
{transcript}

Provide:
1. Brief summary (2-3 sentences)
2. Key points (3-5 bullets)
3. Action items with owners
4. Important decisions
5. Suggested title

RESPOND WITH JSON:
{{
    "executive": "Brief summary of the meeting",
    "bullets": ["Key point 1", "Key point 2", "Key point 3"],
    "actions": [{{"action": "Task", "owner": "Person", "priority": "high"}}],
    "decisions": ["Decision 1"],
    "title": "Meeting title"
}}"""

            try:
                result = await self._call_llm(
                    session_id=session_id,
                    prompt=prompt,
                    max_tokens=500,
                    temperature=0.5,
                    system_prompt="You are a meeting analyst. Provide meeting analysis in valid JSON format.",
                    timeout=120,
                    task="quality",
                )

                if result:
                    json_str = self._extract_json_from_ai_response(result)
                    if json_str:
                        try:
                            analysis_result = json.loads(json_str)
                            logger.info(f"Meeting analysis completed using {self.model_name}")
                            return {
                                "success": True,
                                "analysis": analysis_result,
                                "agent_id": str(agent.id)
                            }
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse extracted JSON: {e}")
                    else:
                        logger.warning("No JSON found in AI response")

                # Fallback to basic analysis
                return await self._create_fallback_analysis(session_id, transcript)

            except asyncio.TimeoutError:
                logger.error(f"AI analysis timed out for session {session_id}")
                return await self._create_fallback_analysis(session_id, transcript)

        except Exception as e:
            logger.error(f"Analysis failed: {e}")
            return await self._create_fallback_analysis(session_id, transcript)
        finally:
            if db_session:
                db_session.close()

    async def _send_text_summary_update(self, session_id: str, session_info: Dict, interval_info: Dict, summary_text: str, processing_time: int):
        """Send plain text summary update via WebSocket"""
        try:
            agent = session_info['agent']
            websocket_callback = session_info['websocket_callback']

            message = {
                "type": "progressive_summary",
                "session_id": session_id,
                "agent": {
                    "id": str(agent.id),
                    "name": agent.name,
                    "role": "unified"
                },
                "summary_data": {
                    "text": summary_text,
                    "word_count": interval_info['current_word_count'],
                    "interval_used": interval_info['current_interval'],
                    "next_interval": interval_info.get('next_interval', interval_info['current_interval']),
                    "model": self.model_name,
                    "processing_time_ms": processing_time
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            await websocket_callback(message)
            logger.info(f"Sent text summary update via WebSocket for session {session_id}")

        except Exception as e:
            logger.error(f"Error sending text summary update: {e}")

    async def _send_summary_update(self, session_id: str, session_info: Dict, interval_info: Dict, summary: Dict, processing_time: int):
        """Send summary update via WebSocket (for structured summaries)"""
        try:
            agent = session_info['agent']
            websocket_callback = session_info['websocket_callback']

            message = {
                "type": "progressive_summary",
                "session_id": session_id,
                "agent": {
                    "id": str(agent.id),
                    "name": agent.name,
                    "role": "unified"
                },
                "summary_data": {
                    "word_count_at_summary": interval_info['current_word_count'],
                    "interval_used": interval_info['current_interval'],
                    "next_interval": interval_info.get('next_interval', interval_info['current_interval']),
                    "model_size": self.model_name,
                    "sections": summary
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            await websocket_callback(message)

        except Exception as e:
            logger.error(f"Error sending summary update: {e}")

    async def _save_progressive_text_summary_to_db(self, session_id: str, interval_info: Dict, summary_text: str, processing_time: int):
        """Save progressive plain text summary to database"""
        try:
            from database.database import get_db_session
            from database.models import RecordingSession

            db = get_db_session()

            # Find session by session_id
            session = db.query(RecordingSession).filter(
                RecordingSession.session_id == session_id
            ).first()

            if not session:
                logger.warning(f"Session {session_id} not found in database for progressive summary save")
                return

            # Create progressive summary entry (plain text format)
            progressive_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "word_count_at_summary": interval_info.get('current_word_count', 0),
                "interval_used": interval_info.get('current_interval', 500),
                "processing_time_ms": processing_time,
                "text": summary_text,
                "model": self.model_name
            }

            # Get existing progressive summaries or initialize empty list
            if session.progressive_summaries:
                summaries = session.progressive_summaries if isinstance(session.progressive_summaries, list) else []
            else:
                summaries = []

            # Append new summary
            summaries.append(progressive_entry)

            # Update session
            session.progressive_summaries = summaries

            # Also update processing_metadata with latest stats
            metadata = session.processing_metadata or {}
            metadata.update({
                "last_progressive_summary": datetime.now(timezone.utc).isoformat(),
                "total_progressive_summaries": len(summaries),
                "current_word_count": interval_info.get('current_word_count', 0),
                "model_used": self.model_name
            })
            session.processing_metadata = metadata

            db.commit()
            logger.info(f"Saved progressive text summary #{len(summaries)} to database for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to save progressive summary to database: {e}")
        finally:
            if db:
                db.close()

    async def _create_fallback_analysis(self, session_id: str, transcript: str) -> Dict:
        """Create basic fallback analysis when AI fails"""
        word_count = len(transcript.split())
        duration_estimate = word_count / 150  # Approximate minutes

        # Extract first few sentences for summary
        sentences = transcript.split('. ')[:3]
        brief_text = '. '.join(sentences)[:200] + "..."

        fallback_analysis = {
            "executive": f"Meeting recorded with {word_count} words transcribed. {brief_text}",
            "bullets": [
                f"Meeting duration: approximately {duration_estimate:.1f} minutes",
                f"Total words transcribed: {word_count}",
                "Meeting content captured successfully",
                "AI analysis temporarily unavailable"
            ],
            "actions": [],
            "decisions": [],
            "title": f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        }

        return {
            "success": True,  # Mark as success even with fallback
            "analysis": fallback_analysis,
            "agent_id": "fallback",
            "fallback": True
        }

    def get_agent_status(self) -> Dict:
        """Get current agent configuration"""
        try:
            db_session = get_db_session()
            agent = UnifiedMeetingAgent.get_default_agent(db_session)

            if agent:
                return {
                    "agent": agent.to_dict(),
                    "active_sessions": len(self.active_sessions),
                    "model_ready": True
                }
            else:
                return {
                    "agent": None,
                    "active_sessions": 0,
                    "model_ready": False
                }
        except Exception as e:
            logger.error(f"Error getting agent status: {e}")
            return {"error": str(e)}
        finally:
            if db_session:
                db_session.close()

# Global instance
unified_agent_service = UnifiedAgentService()


# ---------------------------------------------------------------------------
# Reusable, session-state-free meeting titler.
#
# `UnifiedAgentService._generate_meeting_title` is bound to the live
# `active_sessions` map and re-reads the transcript via the live buffer, so
# it only works while a meeting is being monitored in-process. Background
# jobs (the always-on finalize worker) and the backfill script need to
# title a *finished* session from its stored transcript without any live
# state. These module-level helpers extract that LLM titling pattern into a
# pure function that takes the transcript + org_id directly and routes
# through the same org-aware ProviderRegistry "fast" task.
# ---------------------------------------------------------------------------

# The default auto-generated always-on title shape is
# `f"Always-on {started_at.strftime('%Y-%m-%d %H:%M')}"` (see
# api/recording.py start_always_on). A session still wearing this title
# (or an empty/null/"untitled" placeholder) has never been given a real
# title and is a candidate for (re)titling.
_DEFAULT_TITLE_RE = re.compile(r"^\s*always-on\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*$", re.IGNORECASE)
_PLACEHOLDER_TITLES = {"", "untitled", "meeting", "recording", "new recording", "new meeting"}


def is_default_session_title(title: Optional[str]) -> bool:
    """True when `title` is an auto-generated placeholder (the dated
    "Always-on YYYY-MM-DD HH:MM" default, empty/null, or a generic
    placeholder), i.e. the session has never received a real title."""
    if title is None:
        return True
    stripped = title.strip()
    if stripped.lower() in _PLACEHOLDER_TITLES:
        return True
    return bool(_DEFAULT_TITLE_RE.match(stripped))


# Titles the LLM might echo back that are no better than the default; reject
# them so we keep the existing default rather than overwrite it with noise.
_REJECTED_TITLE_VALUES = {"untitled", "meeting", "recording", "title", "meeting title"}


def _clean_generated_title(raw: Optional[str]) -> Optional[str]:
    """Normalize an LLM title response: strip quotes/newlines/markdown,
    drop a leading "Title:" label, cap length. Returns None when the
    result is empty or a rejected placeholder value."""
    if not raw:
        return None
    title = raw.replace('"', "").replace("'", "").replace("\n", " ").strip()
    # Strip a leading "Title:" / "Meeting title:" label the model may add.
    title = re.sub(r"^\s*(?:meeting\s+)?title\s*[:\-]\s*", "", title, flags=re.IGNORECASE).strip()
    # Strip surrounding markdown emphasis / stray backticks.
    title = title.strip("*`# ").strip()
    if not title:
        return None
    if title.lower() in _REJECTED_TITLE_VALUES:
        return None
    return title[:200]


def _build_title_prompt(transcript: str) -> str:
    """Title prompt mirroring UnifiedAgentService._generate_meeting_title,
    but tolerant of very short transcripts (best-effort title from
    whatever was said)."""
    snippet = transcript[:4000]
    return (
        "Based on this meeting transcript, generate a concise, descriptive "
        "meeting title (4-8 words). The transcript may be very short — still "
        "produce a sensible best-effort title from whatever was said.\n\n"
        f"{snippet}\n\n"
        "The title should capture the main purpose/topic. Examples:\n"
        '- "Q4 Budget Planning Review"\n'
        '- "Product Launch Strategy Meeting"\n'
        '- "Weekly Team Standup"\n\n'
        "RESPOND WITH ONLY THE TITLE:"
    )


async def generate_title_from_transcript(
    org_id: int,
    transcript: str,
    *,
    timeout: int = 30,
) -> Optional[str]:
    """Generate a concise meeting title from a stored transcript, routed
    through the org's ProviderRegistry "fast" task.

    Pure / session-state-free: safe to call from background jobs and
    scripts. Returns a cleaned title, or None on empty transcript / LLM
    failure / unusable response. Never raises — provider errors are
    logged and swallowed (callers treat None as "leave the default").
    """
    if not transcript or not transcript.strip():
        return None
    if org_id is None:
        logger.warning("generate_title_from_transcript: org_id is None; cannot route LLM call")
        return None

    prompt = _build_title_prompt(transcript)
    system_prompt = "You generate concise meeting titles. Respond with ONLY the title."

    def _do_call() -> Optional[str]:
        db = SessionLocal()
        try:
            registry = get_provider_registry(db)
            provider = registry.get_llm(org_id=org_id, task="fast")
        except Exception as exc:
            logger.error(
                "generate_title_from_transcript: registry resolution failed for org %s: %s",
                org_id, exc, exc_info=True,
            )
            return None
        finally:
            db.close()
        try:
            return provider.chat_sync(
                system_prompt,
                prompt,
                max_tokens=30,
                temperature=0.3,
            )
        except Exception as exc:
            logger.error(
                "generate_title_from_transcript: provider chat_sync failed for org %s: %s",
                org_id, exc, exc_info=True,
            )
            return None

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_do_call), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("generate_title_from_transcript: LLM call timed out after %ss (org %s)", timeout, org_id)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("generate_title_from_transcript: unexpected error for org %s: %s", org_id, exc, exc_info=True)
        return None

    return _clean_generated_title(raw)


def generate_title_from_transcript_sync(
    org_id: int,
    transcript: str,
    *,
    timeout: int = 30,
) -> Optional[str]:
    """Synchronous wrapper around `generate_title_from_transcript` for use
    from plain sync scripts (the backfill). Returns the same values."""
    if not transcript or not transcript.strip():
        return None
    if org_id is None:
        return None

    prompt = _build_title_prompt(transcript)
    system_prompt = "You generate concise meeting titles. Respond with ONLY the title."

    db = SessionLocal()
    try:
        registry = get_provider_registry(db)
        provider = registry.get_llm(org_id=org_id, task="fast")
    except Exception as exc:
        logger.error(
            "generate_title_from_transcript_sync: registry resolution failed for org %s: %s",
            org_id, exc, exc_info=True,
        )
        return None
    finally:
        db.close()

    try:
        raw = provider.chat_sync(
            system_prompt,
            prompt,
            max_tokens=30,
            temperature=0.3,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "generate_title_from_transcript_sync: provider chat_sync failed for org %s: %s",
            org_id, exc, exc_info=True,
        )
        return None

    return _clean_generated_title(raw)
