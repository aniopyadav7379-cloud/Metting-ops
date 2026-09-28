"""
Performance Metrics Service
==========================
Tracks and analyzes model switching performance, NPU acceleration metrics,
and transcription efficiency.
"""

import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from collections import defaultdict
import json
import statistics

from sqlalchemy.orm import Session
from database.database import get_db
from database.models import Settings

logger = logging.getLogger(__name__)

@dataclass
class ModelPerformanceMetric:
    """Single performance measurement for a model"""
    timestamp: datetime
    session_id: str
    model_id: str
    model_name: str
    audio_duration: float  # seconds
    processing_time: float  # seconds
    rtf: float  # Real-time factor (processing_time / audio_duration)
    npu_accelerated: bool
    confidence_avg: float
    words_transcribed: int
    speakers_detected: int
    memory_usage_mb: Optional[float] = None
    cpu_usage_percent: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        result = asdict(self)
        result['timestamp'] = self.timestamp.isoformat()
        return result

class PerformanceMetricsService:
    """Service for collecting and analyzing model performance metrics"""
    
    def __init__(self):
        self.metrics_buffer: List[ModelPerformanceMetric] = []
        self.buffer_size = 1000  # Keep last 1000 metrics in memory
        
        # Performance aggregations
        self.model_stats = defaultdict(lambda: {
            'total_audio_processed': 0.0,
            'total_processing_time': 0.0,
            'invocation_count': 0,
            'rtf_samples': [],
            'confidence_samples': [],
            'last_used': None
        })
        
        logger.info("📊 Performance Metrics Service initialized")
    
    def record_model_performance(
        self,
        session_id: str,
        model_id: str,
        model_name: str,
        audio_duration: float,
        processing_time: float,
        npu_accelerated: bool = False,
        confidence_avg: float = 0.0,
        words_transcribed: int = 0,
        speakers_detected: int = 0,
        memory_usage_mb: Optional[float] = None,
        cpu_usage_percent: Optional[float] = None
    ):
        """Record a performance measurement for a model"""
        try:
            rtf = processing_time / audio_duration if audio_duration > 0 else 0
            
            metric = ModelPerformanceMetric(
                timestamp=datetime.now(timezone.utc),
                session_id=session_id,
                model_id=model_id,
                model_name=model_name,
                audio_duration=audio_duration,
                processing_time=processing_time,
                rtf=rtf,
                npu_accelerated=npu_accelerated,
                confidence_avg=confidence_avg,
                words_transcribed=words_transcribed,
                speakers_detected=speakers_detected,
                memory_usage_mb=memory_usage_mb,
                cpu_usage_percent=cpu_usage_percent
            )
            
            # Add to buffer
            self.metrics_buffer.append(metric)
            if len(self.metrics_buffer) > self.buffer_size:
                self.metrics_buffer.pop(0)
            
            # Update aggregated stats
            stats = self.model_stats[model_id]
            stats['total_audio_processed'] += audio_duration
            stats['total_processing_time'] += processing_time
            stats['invocation_count'] += 1
            stats['rtf_samples'].append(rtf)
            stats['confidence_samples'].append(confidence_avg)
            stats['last_used'] = datetime.now(timezone.utc)
            
            # Keep only last 100 samples for efficiency
            if len(stats['rtf_samples']) > 100:
                stats['rtf_samples'] = stats['rtf_samples'][-100:]
            if len(stats['confidence_samples']) > 100:
                stats['confidence_samples'] = stats['confidence_samples'][-100:]
            
            # Persist to database periodically
            if len(self.metrics_buffer) % 10 == 0:
                self._persist_metrics()
                
            logger.debug(f"📈 Recorded performance: {model_id} RTF={rtf:.3f}")
            
        except Exception as e:
            logger.error(f"Error recording performance metric: {e}")
    
    def get_model_performance_summary(self, model_id: str) -> Dict[str, Any]:
        """Get performance summary for a specific model"""
        try:
            stats = self.model_stats.get(model_id)
            if not stats or stats['invocation_count'] == 0:
                return {
                    "model_id": model_id,
                    "error": "No performance data available"
                }
            
            rtf_samples = stats['rtf_samples']
            confidence_samples = stats['confidence_samples']
            
            return {
                "model_id": model_id,
                "statistics": {
                    "total_audio_hours": round(stats['total_audio_processed'] / 3600, 2),
                    "total_processing_time": round(stats['total_processing_time'], 2),
                    "invocation_count": stats['invocation_count'],
                    "average_rtf": round(statistics.mean(rtf_samples), 4),
                    "median_rtf": round(statistics.median(rtf_samples), 4),
                    "best_rtf": round(min(rtf_samples), 4),
                    "worst_rtf": round(max(rtf_samples), 4),
                    "rtf_std_dev": round(statistics.stdev(rtf_samples), 4) if len(rtf_samples) > 1 else 0,
                    "average_confidence": round(statistics.mean(confidence_samples), 3) if confidence_samples else 0,
                    "speedup_factor": round(1 / statistics.mean(rtf_samples), 1) if rtf_samples else 0,
                    "last_used": stats['last_used'].isoformat() if stats['last_used'] else None
                }
            }
        except Exception as e:
            logger.error(f"Error getting model performance summary: {e}")
            return {"model_id": model_id, "error": str(e)}
    
    def get_all_models_performance(self) -> Dict[str, Any]:
        """Get performance comparison across all models"""
        try:
            models_performance = {}
            total_stats = {
                "total_audio_processed": 0,
                "total_models_tested": 0,
                "npu_models_count": 0,
                "cpu_models_count": 0
            }
            
            for model_id in self.model_stats.keys():
                model_summary = self.get_model_performance_summary(model_id)
                if "error" not in model_summary:
                    models_performance[model_id] = model_summary
                    
                    # Aggregate totals
                    stats = model_summary["statistics"]
                    total_stats["total_audio_processed"] += stats["total_audio_hours"]
                    total_stats["total_models_tested"] += 1
                    
                    # Determine if NPU model based on model_id
                    if "npu" in model_id.lower() or "whisperx" in model_id.lower():
                        total_stats["npu_models_count"] += 1
                    else:
                        total_stats["cpu_models_count"] += 1
            
            # Find best performing models
            best_speed = None
            best_accuracy = None
            
            for model_id, data in models_performance.items():
                stats = data["statistics"]
                rtf = stats["average_rtf"]
                confidence = stats["average_confidence"]
                
                if best_speed is None or rtf < best_speed["rtf"]:
                    best_speed = {"model_id": model_id, "rtf": rtf, "speedup": stats["speedup_factor"]}
                    
                if best_accuracy is None or confidence > best_accuracy["confidence"]:
                    best_accuracy = {"model_id": model_id, "confidence": confidence}
            
            return {
                "models": models_performance,
                "summary": {
                    **total_stats,
                    "best_speed_model": best_speed,
                    "best_accuracy_model": best_accuracy,
                    "measurement_period": "last_1000_operations"
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting all models performance: {e}")
            return {"error": str(e)}
    
    def get_recent_metrics(self, hours: int = 24, model_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get recent performance metrics"""
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
            
            filtered_metrics = []
            for metric in self.metrics_buffer:
                if metric.timestamp >= cutoff_time:
                    if model_id is None or metric.model_id == model_id:
                        filtered_metrics.append(metric.to_dict())
            
            # Sort by timestamp descending
            filtered_metrics.sort(key=lambda x: x['timestamp'], reverse=True)
            
            return filtered_metrics
            
        except Exception as e:
            logger.error(f"Error getting recent metrics: {e}")
            return []
    
    def get_performance_trends(self, model_id: str, hours: int = 24) -> Dict[str, Any]:
        """Analyze performance trends over time"""
        try:
            recent_metrics = self.get_recent_metrics(hours, model_id)
            
            if len(recent_metrics) < 2:
                return {
                    "model_id": model_id,
                    "message": "Insufficient data for trend analysis",
                    "metrics_count": len(recent_metrics)
                }
            
            # Group metrics by hour for trend analysis
            hourly_stats = defaultdict(list)
            
            for metric in recent_metrics:
                timestamp = datetime.fromisoformat(metric['timestamp'])
                hour_key = timestamp.replace(minute=0, second=0, microsecond=0)
                hourly_stats[hour_key].append(metric)
            
            # Calculate trends
            hourly_trends = []
            for hour, metrics in sorted(hourly_stats.items()):
                rtf_values = [m['rtf'] for m in metrics]
                confidence_values = [m['confidence_avg'] for m in metrics]
                
                hourly_trends.append({
                    "hour": hour.isoformat(),
                    "metrics_count": len(metrics),
                    "average_rtf": round(statistics.mean(rtf_values), 4),
                    "average_confidence": round(statistics.mean(confidence_values), 3),
                    "total_audio_seconds": sum(m['audio_duration'] for m in metrics)
                })
            
            # Calculate overall trend direction
            if len(hourly_trends) >= 2:
                recent_rtf = statistics.mean([h['average_rtf'] for h in hourly_trends[-2:]])
                older_rtf = statistics.mean([h['average_rtf'] for h in hourly_trends[:2]])
                
                rtf_trend = "improving" if recent_rtf < older_rtf else "declining" if recent_rtf > older_rtf else "stable"
            else:
                rtf_trend = "stable"
            
            return {
                "model_id": model_id,
                "trend_analysis": {
                    "rtf_trend": rtf_trend,
                    "hours_analyzed": hours,
                    "total_metrics": len(recent_metrics),
                    "hourly_breakdown": hourly_trends
                }
            }
            
        except Exception as e:
            logger.error(f"Error analyzing performance trends: {e}")
            return {"model_id": model_id, "error": str(e)}
    
    def compare_models(self, model_ids: List[str]) -> Dict[str, Any]:
        """Compare performance between multiple models"""
        try:
            comparison = {}
            
            for model_id in model_ids:
                comparison[model_id] = self.get_model_performance_summary(model_id)
            
            # Find winners in each category
            winners = {
                "fastest": {"model_id": None, "rtf": float('inf')},
                "most_accurate": {"model_id": None, "confidence": 0},
                "most_used": {"model_id": None, "invocations": 0},
                "most_efficient": {"model_id": None, "hours_per_second": 0}
            }
            
            for model_id, data in comparison.items():
                if "error" in data:
                    continue
                    
                stats = data["statistics"]
                
                # Fastest (lowest RTF)
                if stats["average_rtf"] < winners["fastest"]["rtf"]:
                    winners["fastest"] = {"model_id": model_id, "rtf": stats["average_rtf"]}
                
                # Most accurate
                if stats["average_confidence"] > winners["most_accurate"]["confidence"]:
                    winners["most_accurate"] = {"model_id": model_id, "confidence": stats["average_confidence"]}
                
                # Most used
                if stats["invocation_count"] > winners["most_used"]["invocations"]:
                    winners["most_used"] = {"model_id": model_id, "invocations": stats["invocation_count"]}
                
                # Most efficient (hours processed per processing second)
                if stats["total_processing_time"] > 0:
                    efficiency = stats["total_audio_hours"] / (stats["total_processing_time"] / 3600)
                    if efficiency > winners["most_efficient"]["hours_per_second"]:
                        winners["most_efficient"] = {"model_id": model_id, "hours_per_second": efficiency}
            
            return {
                "models": comparison,
                "winners": winners,
                "comparison_timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error comparing models: {e}")
            return {"error": str(e)}
    
    def _persist_metrics(self):
        """Persist metrics buffer to database"""
        try:
            # Store metrics in settings as JSON for now
            # In production, you might want a dedicated metrics table
            db = next(get_db())
            
            # Get existing metrics
            existing_setting = db.query(Settings).filter(
                Settings.key == "performance.metrics_data"
            ).first()
            
            if existing_setting:
                try:
                    existing_metrics = existing_setting.value
                except:
                    existing_metrics = []
            else:
                existing_metrics = []
            
            # Add new metrics (last 10 from buffer)
            new_metrics = [m.to_dict() for m in self.metrics_buffer[-10:]]
            all_metrics = existing_metrics + new_metrics
            
            # Keep only last 500 metrics to prevent database bloat
            if len(all_metrics) > 500:
                all_metrics = all_metrics[-500:]
            
            # Update or create setting
            if existing_setting:
                existing_setting.value = all_metrics
                existing_setting.updated_at = datetime.now(timezone.utc)
            else:
                new_setting = Settings(
                    key="performance.metrics_data",
                    value=all_metrics,
                    category="performance",
                    description="Model performance metrics data"
                )
                db.add(new_setting)
            
            db.commit()
            logger.debug("📊 Persisted performance metrics to database")
            
        except Exception as e:
            logger.error(f"Error persisting metrics: {e}")
        finally:
            db.close()
    
    def load_persisted_metrics(self):
        """Load previously persisted metrics from database"""
        try:
            db = next(get_db())
            
            setting = db.query(Settings).filter(
                Settings.key == "performance.metrics_data"
            ).first()
            
            if setting and setting.value:
                persisted_metrics = setting.value
                
                # Convert back to ModelPerformanceMetric objects
                for metric_data in persisted_metrics[-100:]:  # Load last 100
                    try:
                        metric_data['timestamp'] = datetime.fromisoformat(metric_data['timestamp'])
                        metric = ModelPerformanceMetric(**metric_data)
                        
                        # Update model stats
                        stats = self.model_stats[metric.model_id]
                        stats['total_audio_processed'] += metric.audio_duration
                        stats['total_processing_time'] += metric.processing_time
                        stats['invocation_count'] += 1
                        stats['rtf_samples'].append(metric.rtf)
                        stats['confidence_samples'].append(metric.confidence_avg)
                        stats['last_used'] = metric.timestamp
                        
                    except Exception as e:
                        logger.warning(f"Error loading persisted metric: {e}")
                        continue
                
                logger.info(f"📊 Loaded {len(persisted_metrics)} persisted performance metrics")
                
        except Exception as e:
            logger.error(f"Error loading persisted metrics: {e}")
        finally:
            db.close()

# Global performance metrics service instance
performance_metrics_service = PerformanceMetricsService()

# Load persisted metrics on startup
performance_metrics_service.load_persisted_metrics()