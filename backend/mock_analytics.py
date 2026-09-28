#!/usr/bin/env python3
"""
Mock analytics data for testing
"""

from datetime import datetime, timedelta
import random
from typing import Dict, List, Any

def generate_mock_analytics(time_range: str = 'month') -> Dict[str, Any]:
    """Generate mock analytics data for testing"""
    
    # Calculate date range
    end_date = datetime.utcnow()
    if time_range == 'week':
        days = 7
    elif time_range == 'month':
        days = 30
    elif time_range == 'quarter':
        days = 90
    elif time_range == 'year':
        days = 365
    else:
        days = 30
    
    start_date = end_date - timedelta(days=days)
    
    # Generate summary
    total_meetings = random.randint(days // 3, days * 2)
    avg_duration = random.randint(1800, 5400)  # 30-90 minutes
    total_duration = total_meetings * avg_duration
    
    summary = {
        "totalMeetings": total_meetings,
        "totalDuration": total_duration,
        "averageDuration": avg_duration,
        "totalTranscriptions": total_meetings * random.randint(5, 20),
        "totalSpeakers": random.randint(10, 50),
        "npuUsage": random.randint(60, 95)
    }
    
    # Generate meetings by day
    meetings_by_day = []
    current_date = start_date
    while current_date <= end_date:
        count = random.randint(0, 5) if current_date.weekday() < 5 else random.randint(0, 1)
        meetings_by_day.append({
            "date": current_date.strftime("%Y-%m-%d"),
            "count": count,
            "duration": count * random.randint(1800, 5400)
        })
        current_date += timedelta(days=1)
    
    # Generate meeting types
    meeting_types = [
        {"type": "Team Standup", "count": int(total_meetings * 0.3), "percentage": 30},
        {"type": "Client Meeting", "count": int(total_meetings * 0.25), "percentage": 25},
        {"type": "Project Review", "count": int(total_meetings * 0.2), "percentage": 20},
        {"type": "One on One", "count": int(total_meetings * 0.15), "percentage": 15},
        {"type": "All Hands", "count": int(total_meetings * 0.1), "percentage": 10}
    ]
    
    # Generate speaker stats
    speaker_stats = []
    for i in range(10):
        time = random.randint(3600, 36000)  # 1-10 hours
        speaker_stats.append({
            "speaker": f"Speaker_{i+1:02d}",
            "totalTime": time,
            "percentage": random.randint(5, 20)
        })
    speaker_stats.sort(key=lambda x: x["totalTime"], reverse=True)
    
    # Generate performance metrics
    performance_metrics = []
    for i in range(min(30, days)):
        date = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
        performance_metrics.append({
            "date": date,
            "processingTime": round(random.uniform(0.5, 5.0), 2),
            "rtf": round(random.uniform(0.01, 0.1), 3),
            "npuAcceleration": random.choice([True, True, True, False])  # 75% NPU
        })
    performance_metrics.reverse()
    
    # Generate top keywords
    keywords = [
        "project", "development", "meeting", "review", "client",
        "update", "timeline", "budget", "requirements", "testing",
        "release", "sprint", "backlog", "roadmap", "milestone"
    ]
    top_keywords = []
    for i, word in enumerate(random.sample(keywords, min(12, len(keywords)))):
        top_keywords.append({
            "word": word,
            "count": random.randint(50 - i * 4, 100 - i * 5)
        })
    
    return {
        "summary": summary,
        "meetingsByDay": meetings_by_day,
        "meetingsByType": meeting_types,
        "speakerStats": speaker_stats,
        "performanceMetrics": performance_metrics,
        "topKeywords": top_keywords
    }

# FastAPI endpoint for mock data
from fastapi import APIRouter, Query

mock_analytics_router = APIRouter(prefix="/api/analytics", tags=["analytics"])

@mock_analytics_router.get("/meetings")
async def get_mock_meeting_analytics(
    range: str = Query("month", description="Time range: week, month, quarter, year")
):
    """Get mock meeting analytics for testing"""
    return generate_mock_analytics(range)

@mock_analytics_router.get("/session/{session_id}/trends")
async def get_mock_session_trends(session_id: str):
    """Get mock session trends for testing"""
    return {
        "timeline": [
            {"timestamp": f"2025-01-24T10:{i:02d}:00", "speaker": f"Speaker_{i % 3}", 
             "duration": random.randint(10, 60), "confidence": random.uniform(0.8, 0.99)}
            for i in range(0, 60, 5)
        ],
        "speakerBalance": [
            {"speaker": f"Speaker_{i}", "percentage": random.randint(20, 40), 
             "segments": random.randint(10, 30)}
            for i in range(3)
        ],
        "totalDuration": 3600,
        "averageConfidence": 0.92
    }