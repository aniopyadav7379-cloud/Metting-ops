"""
AI-powered meeting insights API endpoints
Uses real speaker data from transcription segments and LLM for analysis.
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel
from sqlalchemy.orm import Session
import asyncio
import logging
import json

from database.database import get_db
from database.models import RecordingSession as DBRecordingSession, Transcription
from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from auth.models import User
from auth.tier import gate_feature_for_caller
from services.providers import get_provider_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/simple/recording-sessions", tags=["insights"])

def _resolve_llm(db: Session, org_id: Optional[int], task: str = "quality"):
    """Resolve the org-aware LLM provider. Returns None if no org context or if
    registry resolution fails — callers gracefully degrade to non-LLM output.

    Silent fallback to a default provider is intentionally NOT done: it would
    bypass per-org provider config and billing, which is unacceptable in the
    multi-tenant model."""
    if org_id is None:
        return None
    try:
        registry = get_provider_registry(db)
        return registry.get_llm(org_id=org_id, task=task)
    except Exception as exc:
        logger.error(
            f"ProviderRegistry LLM resolution failed for org_id={org_id} task={task}: {exc}",
            exc_info=True,
        )
        return None


class ActionItem(BaseModel):
    id: str
    text: str
    assignee: Optional[str] = None
    priority: str = "medium"
    dueDate: Optional[str] = None
    completed: bool = False
    category: str = "general"

class KeywordTrend(BaseModel):
    word: str
    frequency: int
    trend: str = "stable"
    category: str = "topic"

class SpeakerInsight(BaseModel):
    speaker: str
    talkTime: int
    wordCount: int
    sentiment: str = "neutral"
    engagement: float = 0.5
    keyTopics: List[str] = []

class MeetingInsightsResponse(BaseModel):
    summary: str
    keywords: List[KeywordTrend]
    action_items: List[ActionItem]
    speaker_insights: List[SpeakerInsight]
    sentiment: Dict[str, float]
    duration: int
    topics: List[str]
    key_decisions: List[str]
    follow_ups: List[str]

@router.get("/{session_id}/insights", response_model=MeetingInsightsResponse)
async def get_meeting_insights(
    session_id: str,
    regenerate: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Get AI-generated insights for a recording session.

    Cached in the session's `ai_insights` JSON column — first call hydrates
    it (one LLM call, ~30-40s on Qwen 3.6 35B / P40), subsequent calls
    return the cache in milliseconds. Pass ?regenerate=true to force a
    fresh extraction (used by the Re-process flow and the explicit
    regenerate endpoint).

    Looks up sessions by session_id (UUID string) first, then by integer id.
    Requires authentication.
    """
    try:
        # Canonical resolver: active-org first, then the cross-org
        # has_session_access fallback.
        from api.session_permissions import resolve_session_for_user

        session = resolve_session_for_user(
            db, active_org.organization.id, session_id, current_user
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Get all transcription segments for the session
        transcriptions = db.query(Transcription).filter(
            Transcription.session_id == session.id
        ).order_by(Transcription.start_time).all()
        
        if not transcriptions:
            # Return default insights if no transcription segments exist
            return MeetingInsightsResponse(
                summary="No transcription available yet. Insights will be generated once the recording has content.",
                keywords=[],
                action_items=[],
                speaker_insights=[],
                sentiment={"positive": 0, "neutral": 100, "negative": 0},
                duration=int(session.duration or 0),
                topics=[],
                key_decisions=[],
                follow_ups=[],
            )
        
        # Build full transcript text using the real speaker column
        full_transcript = "\n".join([
            f"[{t.speaker or 'Unknown'}]: {t.text}"
            for t in transcriptions if t.text
        ])

        # Return cached insights when present unless the caller explicitly
        # asked for regeneration. Each fresh extraction triggers a ~30-40s
        # Qwen call on the P40 — caching turns subsequent page loads into
        # millisecond DB reads.
        cached = session.ai_insights
        if cached and not regenerate and isinstance(cached, dict):
            try:
                return MeetingInsightsResponse(**cached)
            except Exception as exc:
                logger.warning(
                    "Cached insights for session %s failed validation, regenerating: %s",
                    session.id, exc,
                )

        # v3.18.2 tier gate: insight generation runs the server LLM.
        # Cached reads above stay open (cache only exists if a paid run
        # generated it earlier), but actual generation is paid-tier.
        gate_feature_for_caller(current_user, "canonical_reprocess", active_org)

        # Generate insights using the org's configured LLM
        insights = await _generate_ai_insights(
            full_transcript,
            transcriptions,
            session,
            db,
            active_org.organization.id,
        )
        # Persist for the next page load.
        try:
            session.ai_insights = insights.model_dump()
            db.commit()
        except Exception as exc:
            logger.warning("Failed to cache insights for session %s: %s", session.id, exc)
            db.rollback()
        return insights
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get insights for session {session_id}: {e}")
        # Return basic insights on error
        return MeetingInsightsResponse(
            summary="Unable to generate AI insights at this time.",
            keywords=[],
            action_items=[],
            speaker_insights=[],
            sentiment={"positive": 0, "neutral": 100, "negative": 0},
            duration=0,
            topics=[],
            key_decisions=[],
            follow_ups=[],
        )

@router.post("/{session_id}/insights/regenerate")
async def regenerate_insights(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Force regeneration of AI insights for a session. Requires authentication."""
    return await get_meeting_insights(
        session_id=session_id,
        regenerate=True,
        db=db,
        current_user=current_user,
        active_org=active_org,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _generate_ai_insights(
    transcript: str,
    transcriptions: List[Transcription],
    session: DBRecordingSession,
    db: Session,
    org_id: Optional[int],
) -> MeetingInsightsResponse:
    """Generate AI insights from transcript using real speaker data and the
    org-aware LLM provider (with legacy fallback)."""

    duration = int(session.duration or 0)

    # ---- Speaker statistics from actual transcription rows ----
    speaker_stats: Dict[str, Dict[str, Any]] = {}
    for t in transcriptions:
        speaker = t.speaker or "Unknown"
        if speaker not in speaker_stats:
            speaker_stats[speaker] = {"time": 0.0, "words": 0, "segments": []}

        if t.end_time and t.start_time:
            speaker_stats[speaker]["time"] += (t.end_time - t.start_time)

        if t.text:
            speaker_stats[speaker]["words"] += len(t.text.split())
            speaker_stats[speaker]["segments"].append(t.text)

    # ---- Keyword extraction (LLM if available, else frequency-based) ----
    keywords = await _extract_keywords(transcript, transcriptions, db, org_id)

    # ---- Build speaker insights with per-speaker sentiment ----
    speaker_insights = []
    for speaker, stats in speaker_stats.items():
        sentiment = await _analyze_speaker_sentiment(stats["segments"], db, org_id)

        speaker_topics = []
        speaker_text = " ".join(stats["segments"]).lower()
        for kw in keywords[:5]:
            if kw["word"] in speaker_text:
                speaker_topics.append(kw["word"])

        speaker_insights.append(SpeakerInsight(
            speaker=speaker,
            talkTime=int(stats["time"]),
            wordCount=stats["words"],
            sentiment=sentiment,
            engagement=min(1.0, stats["words"] / 1000),
            keyTopics=speaker_topics[:3],
        ))

    # ---- Overall sentiment via LLM ----
    sentiment_dist = await _analyze_overall_sentiment(transcript, transcriptions, db, org_id)

    # ---- Summary via LLM ----
    summary = await _generate_summary(transcript, db, org_id)

    # ---- Action items via LLM ----
    action_items = await _extract_action_items(transcript, db, org_id)

    # ---- Key decisions and follow-ups via LLM ----
    key_decisions, follow_ups = await _extract_decisions(transcript, db, org_id)

    # Topics from keyword categories
    topics = list(set(k["word"] for k in keywords[:10]))

    return MeetingInsightsResponse(
        summary=summary,
        keywords=[KeywordTrend(**k) for k in keywords],
        action_items=action_items,
        speaker_insights=speaker_insights,
        sentiment=sentiment_dist,
        duration=duration,
        topics=topics[:5],
        key_decisions=key_decisions[:5],
        follow_ups=follow_ups[:5],
    )


# ---------------------------------------------------------------------------
# LLM-powered helpers (gracefully degrade to heuristics)
# ---------------------------------------------------------------------------

async def _generate_summary(transcript: str, db: Session, org_id: Optional[int]) -> str:
    """Generate a meeting summary using the org's configured LLM (quality task)."""
    svc = _resolve_llm(db, org_id, task="quality")
    if svc:
        try:
            text = await asyncio.to_thread(
                svc.chat_sync,
                system_prompt=(
                    "You are a meeting assistant. Provide a concise, structured "
                    "meeting summary covering main topics, key decisions, and "
                    "action items. Keep the language clear and direct."
                ),
                user_prompt=(
                    "Summarize this meeting transcript:\n\n" + transcript[:16000]
                ),
                max_tokens=500,
                temperature=0.5,
            )
            if text:
                return text
        except Exception as e:
            logger.warning(f"LLM summary failed: {e}")
    return "Meeting discussion covered multiple topics. AI summary generation unavailable."


async def _extract_keywords(
    transcript: str,
    transcriptions: List[Transcription],
    db: Session,
    org_id: Optional[int],
) -> List[Dict[str, Any]]:
    """Extract keywords using LLM, fall back to frequency analysis."""
    svc = _resolve_llm(db, org_id, task="quality")
    if svc:
        try:
            raw = await asyncio.to_thread(
                svc.chat_sync,
                system_prompt="You extract keywords from meeting transcripts. Return ONLY a JSON array of strings.",
                user_prompt=(
                    "Extract the top 10 important keywords or key phrases from this meeting transcript. "
                    "Return ONLY a JSON array of strings, nothing else.\n\n"
                    f"{transcript[:12000]}"
                ),
                max_tokens=200,
                temperature=0.3,
            )
            if raw.strip():
                # Try to parse JSON array from response
                import re
                # Find first JSON array in response
                match = re.search(r'\[.*?\]', raw, re.DOTALL)
                if match:
                    llm_keywords = json.loads(match.group(0))
                    if isinstance(llm_keywords, list) and llm_keywords:
                        results = []
                        for word in llm_keywords[:15]:
                            word_str = str(word).strip().lower()
                            if len(word_str) > 2:
                                # Count actual frequency in transcript
                                freq = transcript.lower().count(word_str)
                                category = "topic"
                                if any(w in word_str for w in ["urgent", "important", "critical", "priority"]):
                                    category = "action"
                                elif any(w in word_str for w in ["code", "api", "database", "system", "server"]):
                                    category = "technical"
                                results.append({
                                    "word": word_str,
                                    "frequency": max(freq, 1),
                                    "trend": "stable",
                                    "category": category,
                                })
                        if results:
                            return results
        except Exception as e:
            logger.warning(f"LLM keyword extraction failed: {e}")

    # Fallback: simple frequency analysis
    return _frequency_keywords(transcriptions)


def _frequency_keywords(transcriptions: List[Transcription]) -> List[Dict[str, Any]]:
    """Extract keywords by word frequency."""
    common_words = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those',
        'i', 'you', 'he', 'she', 'it', 'we', 'they', 'what', 'which', 'who',
        'when', 'where', 'why', 'how', 'not', 'no', 'yes', 'so', 'okay', 'um',
        'uh', 'like', 'just', 'also', 'then', 'about', 'some', 'more', 'very',
        'there', 'been', 'into', 'than', 'them', 'each', 'make', 'well',
    }
    word_freq: Dict[str, int] = {}
    for t in transcriptions:
        if t.text:
            for word in t.text.lower().split():
                word = word.strip('.,!?;:"\'()[]{}')
                if len(word) > 3 and word not in common_words:
                    word_freq[word] = word_freq.get(word, 0) + 1

    keywords = []
    for word, freq in sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:15]:
        category = "topic"
        if word in ["urgent", "important", "critical", "priority"]:
            category = "action"
        elif word in ["happy", "concerned", "worried", "excited"]:
            category = "sentiment"
        elif word in ["code", "api", "database", "system", "server"]:
            category = "technical"
        keywords.append({
            "word": word,
            "frequency": freq,
            "trend": "stable",
            "category": category,
        })
    return keywords


async def _analyze_speaker_sentiment(segments: List[str], db: Session, org_id: Optional[int]) -> str:
    """Analyze sentiment for a speaker's segments using the LLM."""
    if not segments:
        return "neutral"

    svc = _resolve_llm(db, org_id, task="fast")
    if svc:
        try:
            combined = " ".join(segments[:50])[:6000]
            raw = await asyncio.to_thread(
                svc.chat_sync,
                system_prompt="You classify sentiment. Respond with ONLY one word: positive, negative, or neutral.",
                user_prompt=(
                    "Classify the overall sentiment of this speaker's dialogue as exactly one word: "
                    "positive, negative, or neutral. Respond with ONLY one word.\n\n"
                    f"{combined}"
                ),
                max_tokens=10,
                temperature=0.1,
            )
            raw_lower = raw.strip().lower()
            for label in ("positive", "negative", "neutral"):
                if label in raw_lower:
                    return label
        except Exception as e:
            logger.debug(f"LLM speaker sentiment failed: {e}")

    # Fallback heuristic
    positive_words = sum(
        1 for seg in segments
        for word in ["good", "great", "excellent", "happy", "success", "agree", "wonderful"]
        if word in seg.lower()
    )
    negative_words = sum(
        1 for seg in segments
        for word in ["problem", "issue", "concern", "bad", "failure", "disagree", "difficult"]
        if word in seg.lower()
    )
    if positive_words > negative_words * 1.5:
        return "positive"
    elif negative_words > positive_words * 1.5:
        return "negative"
    return "neutral"


async def _analyze_overall_sentiment(
    transcript: str,
    transcriptions: List[Transcription],
    db: Session,
    org_id: Optional[int],
) -> Dict[str, float]:
    """Analyze overall meeting sentiment using LLM."""
    svc = _resolve_llm(db, org_id, task="quality")
    if svc:
        try:
            raw = await asyncio.to_thread(
                svc.chat_sync,
                system_prompt="You analyze meeting sentiment. Return ONLY a JSON object with keys: positive, neutral, negative (percentages summing to 100).",
                user_prompt=(
                    "Analyze the sentiment of this meeting transcript. "
                    "Return ONLY a JSON object with three keys: positive, neutral, negative "
                    "where values are percentages that sum to 100.\n\n"
                    f"{transcript[:12000]}"
                ),
                max_tokens=50,
                temperature=0.3,
            )
            if raw.strip():
                import re
                match = re.search(r'\{.*?\}', raw, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    if "positive" in parsed and "neutral" in parsed and "negative" in parsed:
                        total = float(parsed["positive"]) + float(parsed["neutral"]) + float(parsed["negative"])
                        if total > 0:
                            return {
                                "positive": round(float(parsed["positive"]) / total * 100, 1),
                                "neutral": round(float(parsed["neutral"]) / total * 100, 1),
                                "negative": round(float(parsed["negative"]) / total * 100, 1),
                            }
        except Exception as e:
            logger.debug(f"LLM overall sentiment failed: {e}")

    # Fallback heuristic
    total_segments = len(transcriptions) or 1
    positive_count = sum(
        1 for t in transcriptions
        if any(w in (t.text or "").lower() for w in ["good", "great", "excellent", "happy"])
    )
    negative_count = sum(
        1 for t in transcriptions
        if any(w in (t.text or "").lower() for w in ["problem", "issue", "concern", "bad"])
    )
    neutral_count = total_segments - positive_count - negative_count
    return {
        "positive": round(positive_count * 100 / total_segments, 1),
        "neutral": round(neutral_count * 100 / total_segments, 1),
        "negative": round(negative_count * 100 / total_segments, 1),
    }


async def _extract_action_items(transcript: str, db: Session, org_id: Optional[int]) -> List[ActionItem]:
    """Extract action items using the LLM."""
    svc = _resolve_llm(db, org_id, task="quality")
    if svc:
        try:
            raw = await asyncio.to_thread(
                svc.chat_sync,
                system_prompt="You extract action items from meeting transcripts. Return ONLY a JSON array.",
                user_prompt=(
                    "Extract action items from this meeting transcript. "
                    "Return ONLY a JSON array of objects with keys: text, assignee, priority (high/medium/low).\n\n"
                    f"{transcript[:12000]}"
                ),
                max_tokens=500,
                temperature=0.3,
            )
            if raw.strip():
                import re
                match = re.search(r'\[.*?\]', raw, re.DOTALL)
                if match:
                    items = json.loads(match.group(0))
                    results = []
                    for i, item in enumerate(items[:10]):
                        if isinstance(item, dict) and "text" in item:
                            results.append(ActionItem(
                                id=f"action_{i+1}",
                                text=item["text"],
                                assignee=item.get("assignee"),
                                priority=item.get("priority", "medium"),
                                completed=False,
                                category="meeting",
                            ))
                    if results:
                        return results
        except Exception as e:
            logger.warning(f"LLM action item extraction failed: {e}")
    return []


async def _extract_decisions(transcript: str, db: Session, org_id: Optional[int]) -> tuple:
    """Extract key decisions and follow-ups using the LLM."""
    key_decisions: List[str] = []
    follow_ups: List[str] = []

    svc = _resolve_llm(db, org_id, task="quality")
    if svc:
        try:
            raw = await asyncio.to_thread(
                svc.chat_sync,
                system_prompt="You extract decisions and follow-ups from meeting transcripts. Return ONLY a JSON object.",
                user_prompt=(
                    "Extract key decisions and follow-up items from this meeting. "
                    "Return ONLY a JSON object with two keys: decisions (array of strings) "
                    "and follow_ups (array of strings).\n\n"
                    f"{transcript[:12000]}"
                ),
                max_tokens=300,
                temperature=0.3,
            )
            if raw.strip():
                import re
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed.get("decisions"), list):
                        key_decisions = [str(d) for d in parsed["decisions"][:5]]
                    if isinstance(parsed.get("follow_ups"), list):
                        follow_ups = [str(f) for f in parsed["follow_ups"][:5]]
        except Exception as e:
            logger.warning(f"LLM decision extraction failed: {e}")

    return key_decisions, follow_ups
