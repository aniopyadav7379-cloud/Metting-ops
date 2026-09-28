"""
Simple Analytics API for Integration
Provides real analytics data from sessions and transcription segments
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session, load_only
from sqlalchemy import desc, func
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
import logging
import json

# Database imports
from database.database import get_db
from database.models import RecordingSession as DBRecordingSession, Transcription
from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from auth.models import User

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

@router.get("/summary")
async def get_analytics_summary(
    time_range: str = "month",  # week, month, quarter, year
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get analytics summary with real session and speaker data"""
    try:
        # Calculate date range
        now = datetime.now(timezone.utc)
        if time_range == "week":
            start_date = now - timedelta(days=7)
        elif time_range == "month":
            start_date = now - timedelta(days=30)
        elif time_range == "quarter":
            start_date = now - timedelta(days=90)
        elif time_range == "year":
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)  # Default to month
        
        # Get sessions in date range
        sessions = db.query(DBRecordingSession).options(load_only(
            DBRecordingSession.id,
            DBRecordingSession.duration,
            DBRecordingSession.created_at,
            DBRecordingSession.status,
            DBRecordingSession.summary,
        )).filter(
            DBRecordingSession.organization_id == active_org.organization.id,
            DBRecordingSession.created_at >= start_date,
            DBRecordingSession.status.in_(["completed", "processing"])
        ).all()
        
        # Calculate basic stats from session data
        total_sessions = len(sessions)
        total_duration = sum(s.duration or 0 for s in sessions)
        average_duration = total_duration / total_sessions if total_sessions > 0 else 0
        
        # Get session IDs for transcription lookups
        session_ids = [s.id for s in sessions]
        
        # Get real speaker analytics from transcription segments
        speaking_time = []
        total_speakers = 0
        if session_ids:
            speaker_data = db.query(
                Transcription.speaker,
                func.sum(
                    func.coalesce(Transcription.end_time, 0) -
                    func.coalesce(Transcription.start_time, 0)
                ).label("total_time"),
                func.count(Transcription.id).label("segment_count"),
            ).filter(
                Transcription.session_id.in_(session_ids),
                Transcription.speaker.isnot(None),
            ).group_by(Transcription.speaker).order_by(
                desc("total_time")
            ).all()

            unique_speakers = set()
            for row in speaker_data:
                speaker_name = row[0] or "Unknown"
                talk_seconds = float(row[1] or 0)
                seg_count = int(row[2] or 0)
                unique_speakers.add(speaker_name)
                speaking_time.append({
                    "speaker": speaker_name,
                    "duration": round(talk_seconds, 1),
                    "segments": seg_count,
                    "percentage": 0,  # calculated below
                })
            total_speakers = len(unique_speakers)

            # Calculate percentages
            total_speaking = sum(s["duration"] for s in speaking_time)
            if total_speaking > 0:
                for entry in speaking_time:
                    entry["percentage"] = round(entry["duration"] / total_speaking * 100, 1)
        
        # Generate daily meeting trends for the time range
        meeting_trends = []
        days_to_show = 7 if time_range == "week" else min(30, (now - start_date).days + 1)
        
        for i in range(days_to_show):
            day = now - timedelta(days=days_to_show-1-i)
            day_sessions = [s for s in sessions if s.created_at.date() == day.date()]
            day_duration = sum(s.duration or 0 for s in day_sessions)
            
            meeting_trends.append({
                "date": day.strftime("%a") if time_range == "week" else day.strftime("%m/%d"),
                "duration": int(day_duration),
                "sessions": len(day_sessions)
            })
        
        # Count actual action items from session summaries
        action_items = {"total": 0, "completed": 0, "pending": 0, "overdue": 0}
        
        for session in sessions:
            if session.summary:
                try:
                    summary_data = json.loads(session.summary) if isinstance(session.summary, str) else session.summary
                    if isinstance(summary_data, dict) and 'actions' in summary_data:
                        actions = summary_data['actions']
                        if isinstance(actions, list):
                            action_items["total"] += len(actions)
                            action_items["pending"] += len(actions)
                except Exception:
                    continue
        
        # Session stats
        session_stats = {
            "total_sessions": total_sessions,
            "total_duration": int(total_duration),  # in seconds
            "average_duration": round(average_duration, 1),
            "total_speakers": total_speakers,
        }
        
        return {
            "time_range": time_range,
            "generated_at": now.isoformat(),
            "session_stats": session_stats,
            "speaking_time": speaking_time,
            "keywords": [],  # populated via ai_insights endpoint
            "meeting_trends": meeting_trends,
            "action_items": action_items
        }
        
    except Exception as e:
        logger.error(f"Failed to generate analytics summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate analytics summary")

@router.get("/sessions")
async def get_session_analytics(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """Get recent session analytics with real data including speaker counts"""
    try:
        sessions = db.query(DBRecordingSession).options(load_only(
            DBRecordingSession.id,
            DBRecordingSession.session_id,
            DBRecordingSession.name,
            DBRecordingSession.title,
            DBRecordingSession.created_at,
            DBRecordingSession.duration,
            DBRecordingSession.status,
            DBRecordingSession.summary,
        )).filter(
            DBRecordingSession.organization_id == active_org.organization.id,
            DBRecordingSession.status.in_(["completed", "processing"])
        ).order_by(desc(DBRecordingSession.created_at)).limit(limit).all()
        
        session_ids = [session.id for session in sessions]
        speaker_counts = {}
        if session_ids:
            speaker_counts = {
                int(session_id): int(count)
                for session_id, count in db.query(
                    Transcription.session_id,
                    func.count(func.distinct(Transcription.speaker)),
                ).filter(
                    Transcription.session_id.in_(session_ids),
                    Transcription.speaker.isnot(None),
                ).group_by(Transcription.session_id).all()
            }

        result = []
        for session in sessions:

            result.append({
                "id": session.session_id or str(session.id),
                "name": session.name or session.title or f"Session {session.id}",
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "duration": session.duration or 0,
                "speakers": speaker_counts.get(session.id, 0),
                "status": session.status,
                "has_ai_summary": bool(session.summary)
            })
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to get session analytics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get session analytics")

@router.get("/speakers")
async def get_speaker_analytics(
    time_range: str = "month",
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get detailed speaker analytics across sessions"""
    try:
        now = datetime.now(timezone.utc)
        if time_range == "week":
            start_date = now - timedelta(days=7)
        elif time_range == "month":
            start_date = now - timedelta(days=30)
        elif time_range == "quarter":
            start_date = now - timedelta(days=90)
        elif time_range == "year":
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)

        # Get session IDs in range
        session_ids = [
            s.id for s in db.query(DBRecordingSession.id).filter(
                DBRecordingSession.organization_id == active_org.organization.id,
                DBRecordingSession.created_at >= start_date,
                DBRecordingSession.status.in_(["completed", "processing"]),
            ).all()
        ]

        if not session_ids:
            return {"time_range": time_range, "speakers": [], "total_speakers": 0}

        # Aggregate speaker data
        rows = db.query(
            Transcription.speaker,
            func.sum(
                func.coalesce(Transcription.end_time, 0) -
                func.coalesce(Transcription.start_time, 0)
            ).label("total_time"),
            func.count(Transcription.id).label("segment_count"),
            func.count(func.distinct(Transcription.session_id)).label("session_count"),
            func.avg(Transcription.confidence).label("avg_confidence"),
        ).filter(
            Transcription.session_id.in_(session_ids),
            Transcription.speaker.isnot(None),
        ).group_by(Transcription.speaker).order_by(
            desc("total_time")
        ).limit(limit).all()

        speakers = []
        for row in rows:
            speakers.append({
                "speaker": row[0] or "Unknown",
                "total_talk_time": round(float(row[1] or 0), 1),
                "segments": int(row[2] or 0),
                "sessions_appeared": int(row[3] or 0),
                "avg_confidence": round(float(row[4] or 0), 3),
            })

        return {
            "time_range": time_range,
            "speakers": speakers,
            "total_speakers": len(speakers),
        }
    except Exception as e:
        logger.error(f"Failed to get speaker analytics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get speaker analytics")

@router.get("/duration-trends")
async def get_duration_trends(
    time_range: str = "month",
    group_by: str = "day",  # day or week
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get average meeting duration trends grouped by day or week"""
    try:
        now = datetime.now(timezone.utc)
        if time_range == "week":
            start_date = now - timedelta(days=7)
        elif time_range == "month":
            start_date = now - timedelta(days=30)
        elif time_range == "quarter":
            start_date = now - timedelta(days=90)
        elif time_range == "year":
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)

        sessions = db.query(DBRecordingSession).options(load_only(
            DBRecordingSession.created_at,
            DBRecordingSession.duration,
        )).filter(
            DBRecordingSession.organization_id == active_org.organization.id,
            DBRecordingSession.created_at >= start_date,
            DBRecordingSession.status.in_(["completed", "processing"]),
        ).order_by(DBRecordingSession.created_at).all()

        if group_by == "week":
            # Group by ISO week
            buckets: Dict[str, List[float]] = {}
            for s in sessions:
                key = s.created_at.strftime("%Y-W%W")
                buckets.setdefault(key, []).append(s.duration or 0)
            trends = []
            for key in sorted(buckets.keys()):
                vals = buckets[key]
                trends.append({
                    "period": key,
                    "sessions": len(vals),
                    "total_duration": round(sum(vals), 1),
                    "avg_duration": round(sum(vals) / len(vals), 1) if vals else 0,
                    "min_duration": round(min(vals), 1) if vals else 0,
                    "max_duration": round(max(vals), 1) if vals else 0,
                })
        else:
            # Group by day
            buckets = {}
            for s in sessions:
                key = s.created_at.strftime("%Y-%m-%d")
                buckets.setdefault(key, []).append(s.duration or 0)
            trends = []
            for key in sorted(buckets.keys()):
                vals = buckets[key]
                trends.append({
                    "period": key,
                    "sessions": len(vals),
                    "total_duration": round(sum(vals), 1),
                    "avg_duration": round(sum(vals) / len(vals), 1) if vals else 0,
                    "min_duration": round(min(vals), 1) if vals else 0,
                    "max_duration": round(max(vals), 1) if vals else 0,
                })

        return {
            "time_range": time_range,
            "group_by": group_by,
            "trends": trends,
        }
    except Exception as e:
        logger.error(f"Failed to get duration trends: {e}")
        raise HTTPException(status_code=500, detail="Failed to get duration trends")

@router.get("/performance")
async def get_performance_metrics(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Throughput metrics derived from this org's completed sessions.

    Only values actually computed from the database are returned. This endpoint
    previously reported a hardcoded NPU block (`speedup_factor: 220`,
    `real_time_factor: 0.0045`, `tokens_per_second: 4789`) annotated as "measured" —
    none of it was measured, and this product has no NPU: live inference runs in the
    user's browser and the completion pass runs on the server. Per-stage processing
    timings are not instrumented yet, so no latency or real-time-factor is reported
    rather than inventing one.
    """
    try:
        # Get recent completed sessions for performance analysis
        recent_sessions = db.query(DBRecordingSession).filter(
            DBRecordingSession.organization_id == active_org.organization.id,
            DBRecordingSession.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
            DBRecordingSession.status == "completed"
        ).all()

        # Calculate real processing metrics
        total_duration = sum(s.duration or 0 for s in recent_sessions)
        avg_duration = total_duration / len(recent_sessions) if recent_sessions else 0

        return {
            "window_days": 7,
            "sessions_processed": len(recent_sessions),
            "total_audio_seconds": round(total_duration, 2),
            "avg_session_duration": round(avg_duration, 2),
            "pipeline": {
                "live_inference": "browser",   # Parakeet 0.6B INT8 (onnxruntime-web)
                "completion_pass": "server",   # Parakeet 1.1B + pyannote
            },
            "processing_latency_instrumented": False,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    except Exception as e:
        logger.error(f"Failed to get performance metrics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get performance metrics")
