#!/usr/bin/env python3
"""
Meeting Analytics Service
Provides analytics and insights on meeting data
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session
import logging
from collections import defaultdict, Counter
import re

from database.models import RecordingSession, Transcription, Speaker
from database.database import get_db

logger = logging.getLogger(__name__)

class AnalyticsService:
    """Service for generating meeting analytics"""
    
    def __init__(self, db_session: Session):
        self.db = db_session
    
    def get_meeting_analytics(self, time_range: str = 'month') -> Dict[str, Any]:
        """Get comprehensive meeting analytics for the specified time range"""
        
        # Calculate date range
        end_date = datetime.now(timezone.utc)
        if time_range == 'week':
            start_date = end_date - timedelta(days=7)
        elif time_range == 'month':
            start_date = end_date - timedelta(days=30)
        elif time_range == 'quarter':
            start_date = end_date - timedelta(days=90)
        elif time_range == 'year':
            start_date = end_date - timedelta(days=365)
        else:
            start_date = end_date - timedelta(days=30)  # Default to month
        
        # Get all sessions in the time range
        sessions = self.db.query(RecordingSession).filter(
            RecordingSession.created_at >= start_date,
            RecordingSession.created_at <= end_date
        ).all()
        
        # Calculate summary statistics
        summary = self._calculate_summary(sessions)
        
        # Get meetings by day
        meetings_by_day = self._get_meetings_by_day(sessions, start_date, end_date)
        
        # Get meetings by type
        meetings_by_type = self._get_meetings_by_type(sessions)
        
        # Get speaker statistics
        speaker_stats = self._get_speaker_stats(sessions)
        
        # Get performance metrics
        performance_metrics = self._get_performance_metrics(sessions)
        
        # Get top keywords
        top_keywords = self._get_top_keywords(sessions)
        
        return {
            "summary": summary,
            "meetingsByDay": meetings_by_day,
            "meetingsByType": meetings_by_type,
            "speakerStats": speaker_stats,
            "performanceMetrics": performance_metrics,
            "topKeywords": top_keywords
        }
    
    def _calculate_summary(self, sessions: List[RecordingSession]) -> Dict[str, Any]:
        """Calculate summary statistics"""
        total_meetings = len(sessions)
        total_duration = sum(s.duration_seconds or 0 for s in sessions)
        avg_duration = total_duration / total_meetings if total_meetings > 0 else 0
        
        # Count total transcriptions
        total_transcriptions = sum(
            self.db.query(func.count(Transcription.id))
            .filter(Transcription.session_id == s.id)
            .scalar() or 0
            for s in sessions
        )
        
        # Count unique speakers
        speaker_ids = set()
        for session in sessions:
            speakers = self.db.query(Speaker).filter(Speaker.session_id == session.id).all()
            speaker_ids.update(s.speaker_id for s in speakers)
        
        # Calculate NPU usage percentage
        npu_sessions = sum(1 for s in sessions if s.npu_accelerated)
        npu_usage = (npu_sessions / total_meetings * 100) if total_meetings > 0 else 0
        
        return {
            "totalMeetings": total_meetings,
            "totalDuration": total_duration,
            "averageDuration": avg_duration,
            "totalTranscriptions": total_transcriptions,
            "totalSpeakers": len(speaker_ids),
            "npuUsage": round(npu_usage, 1)
        }
    
    def _get_meetings_by_day(self, sessions: List[RecordingSession], 
                           start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Get meeting counts and duration by day"""
        meetings_by_day = defaultdict(lambda: {"count": 0, "duration": 0})
        
        for session in sessions:
            day = session.created_at.strftime("%Y-%m-%d")
            meetings_by_day[day]["count"] += 1
            meetings_by_day[day]["duration"] += session.duration_seconds or 0
        
        # Fill in missing days with zeros
        result = []
        current_date = start_date
        while current_date <= end_date:
            day_str = current_date.strftime("%Y-%m-%d")
            data = meetings_by_day.get(day_str, {"count": 0, "duration": 0})
            result.append({
                "date": day_str,
                "count": data["count"],
                "duration": data["duration"]
            })
            current_date += timedelta(days=1)
        
        return result
    
    def _get_meetings_by_type(self, sessions: List[RecordingSession]) -> List[Dict[str, Any]]:
        """Get distribution of meeting types"""
        type_counts = Counter(s.meeting_type or "general" for s in sessions)
        total = sum(type_counts.values())
        
        result = []
        for meeting_type, count in type_counts.most_common():
            result.append({
                "type": meeting_type.replace("_", " ").title(),
                "count": count,
                "percentage": round(count / total * 100, 1) if total > 0 else 0
            })
        
        return result
    
    def _get_speaker_stats(self, sessions: List[RecordingSession]) -> List[Dict[str, Any]]:
        """Get speaker time distribution"""
        speaker_times = defaultdict(float)
        
        for session in sessions:
            speakers = self.db.query(Speaker).filter(Speaker.session_id == session.id).all()
            for speaker in speakers:
                speaker_times[speaker.speaker_id] += speaker.total_time
        
        # Get top 10 speakers by time
        sorted_speakers = sorted(speaker_times.items(), key=lambda x: x[1], reverse=True)[:10]
        total_time = sum(time for _, time in sorted_speakers)
        
        result = []
        for speaker_id, time in sorted_speakers:
            result.append({
                "speaker": speaker_id,
                "totalTime": round(time),
                "percentage": round(time / total_time * 100, 1) if total_time > 0 else 0
            })
        
        return result
    
    def _get_performance_metrics(self, sessions: List[RecordingSession]) -> List[Dict[str, Any]]:
        """Get processing performance metrics over time"""
        metrics_by_day = defaultdict(lambda: {"times": [], "rtfs": [], "npu_count": 0})
        
        for session in sessions:
            day = session.created_at.strftime("%Y-%m-%d")
            
            # Get transcriptions for this session
            transcriptions = self.db.query(Transcription).filter(
                Transcription.session_id == session.id
            ).all()
            
            for trans in transcriptions:
                if trans.processing_time:
                    metrics_by_day[day]["times"].append(trans.processing_time)
                if trans.performance_rtf:
                    metrics_by_day[day]["rtfs"].append(trans.performance_rtf)
                if session.npu_accelerated:
                    metrics_by_day[day]["npu_count"] += 1
        
        # Calculate averages
        result = []
        for day, data in sorted(metrics_by_day.items()):
            avg_time = sum(data["times"]) / len(data["times"]) if data["times"] else 0
            avg_rtf = sum(data["rtfs"]) / len(data["rtfs"]) if data["rtfs"] else 0
            
            result.append({
                "date": day,
                "processingTime": round(avg_time, 2),
                "rtf": round(avg_rtf, 2),
                "npuAcceleration": data["npu_count"] > 0
            })
        
        return result
    
    def _get_top_keywords(self, sessions: List[RecordingSession], top_n: int = 12) -> List[Dict[str, Any]]:
        """Extract top keywords from transcriptions"""
        # Common words to exclude
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "up", "about", "into", "through", "during",
            "before", "after", "above", "below", "between", "under", "again",
            "further", "then", "once", "is", "are", "was", "were", "been", "be",
            "have", "has", "had", "do", "does", "did", "will", "would", "should",
            "could", "may", "might", "must", "can", "this", "that", "these", "those",
            "i", "you", "he", "she", "it", "we", "they", "them", "their", "what",
            "which", "who", "when", "where", "why", "how", "all", "each", "every",
            "some", "any", "few", "more", "most", "other", "such", "no", "nor",
            "not", "only", "own", "same", "so", "than", "too", "very", "just"
        }
        
        word_counts = Counter()
        
        for session in sessions:
            transcriptions = self.db.query(Transcription).filter(
                Transcription.session_id == session.id
            ).all()
            
            for trans in transcriptions:
                if trans.text:
                    # Extract words (simple tokenization)
                    words = re.findall(r'\b\w+\b', trans.text.lower())
                    # Filter out stop words and short words
                    words = [w for w in words if w not in stop_words and len(w) > 3]
                    word_counts.update(words)
        
        # Get top keywords
        result = []
        for word, count in word_counts.most_common(top_n):
            result.append({
                "word": word,
                "count": count
            })
        
        return result
    
    def get_session_trends(self, session_id: int) -> Dict[str, Any]:
        """Get trends and insights for a specific session"""
        session = self.db.query(RecordingSession).filter(
            RecordingSession.id == session_id
        ).first()
        
        if not session:
            return {}
        
        # Get transcriptions timeline
        transcriptions = self.db.query(Transcription).filter(
            Transcription.session_id == session_id
        ).order_by(Transcription.timestamp).all()
        
        # Calculate speaking patterns
        timeline = []
        for trans in transcriptions:
            timeline.append({
                "timestamp": trans.timestamp.isoformat() if trans.timestamp else None,
                "speaker": trans.speaker_id,
                "duration": trans.duration,
                "confidence": trans.confidence
            })
        
        # Get speaker balance
        speakers = self.db.query(Speaker).filter(
            Speaker.session_id == session_id
        ).all()
        
        speaker_balance = []
        total_time = sum(s.total_time for s in speakers)
        for speaker in speakers:
            speaker_balance.append({
                "speaker": speaker.speaker_id,
                "percentage": round(speaker.total_time / total_time * 100, 1) if total_time > 0 else 0,
                "segments": speaker.segment_count
            })
        
        return {
            "timeline": timeline,
            "speakerBalance": speaker_balance,
            "totalDuration": session.duration_seconds,
            "averageConfidence": sum(t.confidence or 0 for t in transcriptions) / len(transcriptions) if transcriptions else 0
        }


# FastAPI endpoint
from fastapi import APIRouter, Depends, Query
from typing import Optional

analytics_router = APIRouter(prefix="/api/analytics", tags=["analytics"])

@analytics_router.get("/meetings")
async def get_meeting_analytics(
    range: str = Query("month", description="Time range: week, month, quarter, year"),
    db: Session = Depends(get_db)
):
    """Get comprehensive meeting analytics"""
    service = AnalyticsService(db)
    return service.get_meeting_analytics(range)

@analytics_router.get("/session/{session_id}/trends")
async def get_session_trends(
    session_id: int,
    db: Session = Depends(get_db)
):
    """Get trends and insights for a specific session"""
    service = AnalyticsService(db)
    return service.get_session_trends(session_id)