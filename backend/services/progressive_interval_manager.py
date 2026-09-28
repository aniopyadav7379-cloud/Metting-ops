"""
Progressive Interval Manager
Handles progressive word interval algorithm for meeting agents
"""
import logging
import time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
import json

logger = logging.getLogger(__name__)

@dataclass
class ProgressiveConfig:
    """Configuration for progressive intervals"""
    initial_word_count: int = 500  # Fixed 500 word intervals
    interval_multiplier: float = 1.0  # No progression
    max_interval: int = 500  # Same as initial
    model_size: str = "granite3.3:8b"
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'ProgressiveConfig':
        """Create from dictionary (from database JSONB)"""
        return cls(
            initial_word_count=data.get('initial_interval', data.get('initialWordCount', 500)),
            interval_multiplier=data.get('multiplier', data.get('intervalMultiplier', 1.0)),
            max_interval=data.get('max_interval', data.get('maxInterval', 500)),
            model_size=data.get('modelSize', 'granite3.3:8b')
        )
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for database storage"""
        return {
            'initialWordCount': self.initial_word_count,
            'intervalMultiplier': self.interval_multiplier,
            'maxInterval': self.max_interval,
            'modelSize': self.model_size
        }

@dataclass
class IntervalState:
    """Current state of intervals for a session/agent"""
    current_interval: int
    last_summary_at: int = 0
    total_summaries: int = 0
    
    def to_dict(self) -> Dict:
        return {
            'current_interval': self.current_interval,
            'last_summary_at': self.last_summary_at,
            'total_summaries': self.total_summaries
        }

class ProgressiveIntervalManager:
    """
    Manages progressive word intervals for meeting agents
    
    Key Innovation: Instead of fixed word counts, intervals grow progressively:
    - Start with quick feedback (50 words)
    - Gradually increase intervals (50 → 75 → 112 → 168...)
    - Cap at maximum to prevent overly long waits
    - Different agents have different progression patterns
    """
    
    def __init__(self):
        self.session_states: Dict[str, IntervalState] = {}  # session_id + agent_id -> state
        self.triggered_intervals: Dict[str, set] = {}  # Track triggered intervals per session
        logger.info("Progressive Interval Manager initialized")
    
    def _get_state_key(self, session_id: str, agent_id: str) -> str:
        """Generate unique key for session/agent combination"""
        return f"{session_id}:{agent_id}"
    
    def initialize_session(self, session_id: str, agent_id: str, config: ProgressiveConfig) -> IntervalState:
        """Initialize progressive interval state for a session/agent"""
        state = IntervalState(current_interval=config.initial_word_count)
        state_key = self._get_state_key(session_id, agent_id)
        self.session_states[state_key] = state
        
        logger.info(f"Initialized progressive intervals for session {session_id}, agent {agent_id}")
        logger.info(f"  Initial interval: {config.initial_word_count} words")
        logger.info(f"  Multiplier: {config.interval_multiplier}x")
        logger.info(f"  Max interval: {config.max_interval} words")
        logger.info(f"  Model size: {config.model_size}")
        
        return state
    
    def should_generate_summary(
        self, 
        session_id: str, 
        agent_id: str, 
        current_word_count: int,
        config: ProgressiveConfig
    ) -> Tuple[bool, Dict]:
        """
        Check if we should generate a summary and return interval info
        
        Returns:
            (should_generate, interval_info)
        """
        state_key = self._get_state_key(session_id, agent_id)
        
        # Get or create state
        if state_key not in self.session_states:
            state = self.initialize_session(session_id, agent_id, config)
        else:
            state = self.session_states[state_key]
        
        # Check if we've reached the threshold
        words_since_last = current_word_count - state.last_summary_at
        should_generate = words_since_last >= state.current_interval
        
        interval_info = {
            'current_word_count': current_word_count,
            'words_since_last_summary': words_since_last,
            'current_interval': state.current_interval,
            'next_interval': state.current_interval,  # Will be updated if summary generated
            'total_summaries': state.total_summaries,
            'model_size': config.model_size,
            'should_generate': should_generate
        }
        
        if should_generate:
            # Update state for next interval
            state.last_summary_at = current_word_count
            state.total_summaries += 1
            
            # Calculate next interval (progressive increase)
            next_interval = min(
                int(state.current_interval * config.interval_multiplier),
                config.max_interval
            )
            state.current_interval = next_interval
            interval_info['next_interval'] = next_interval
            
            logger.info(f"Summary triggered for session {session_id}, agent {agent_id}")
            logger.info(f"  Words since last: {words_since_last} >= {interval_info['current_interval']}")
            logger.info(f"  Total summaries: {state.total_summaries}")
            logger.info(f"  Next interval: {next_interval} words")
        
        return should_generate, interval_info
    
    def get_session_state(self, session_id: str, agent_id: str) -> Optional[IntervalState]:
        """Get current state for a session/agent"""
        state_key = self._get_state_key(session_id, agent_id)
        return self.session_states.get(state_key)
    
    async def trigger_progressive_summary(self, session_id: str, current_word_count: int, force: bool = False):
        """
        Trigger progressive summary for a session
        Called from live_recording_transcription when word thresholds are met
        """
        try:
            # Import unified agent service to trigger the actual summary
            from services.unified_agent_service import unified_agent_service
            
            # Check if session is active in unified agent service
            if session_id not in unified_agent_service.active_sessions:
                logger.warning(f"Session {session_id} not active in unified agent service")
                # Start the analysis if not already running
                success = await unified_agent_service.start_meeting_analysis(session_id)
                if not success:
                    logger.error(f"Failed to start meeting analysis for {session_id}")
                    return False
            
            # Trigger summary generation directly
            logger.info(f"📊 Triggering progressive summary for {session_id} at {current_word_count} words")
            
            # Generate the summary using GPU backend
            interval_info = {
                'current_word_count': current_word_count,
                'current_interval': current_word_count,  # Use current count as interval
                'total_summaries': len(self.triggered_intervals.get(session_id, set()))
            }
            
            # Call the unified agent service to generate summary with GPU
            await unified_agent_service._generate_progressive_summary(session_id, interval_info)
            
            # Also broadcast the trigger event for frontend
            from api.websocket_auto_summary import broadcast_progressive_summary
            await broadcast_progressive_summary(session_id, {
                "type": "progressive_trigger",
                "word_count": current_word_count,
                "timestamp": time.time()
            })
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to trigger progressive summary: {e}")
            return False
    
    def reset_session(self, session_id: str, agent_id: str = None):
        """Reset state for a session (all agents or specific agent)"""
        if agent_id:
            # Reset specific agent
            state_key = self._get_state_key(session_id, agent_id)
            if state_key in self.session_states:
                del self.session_states[state_key]
                logger.info(f"Reset progressive state for session {session_id}, agent {agent_id}")
        else:
            # Reset all agents for this session
            keys_to_remove = [key for key in self.session_states.keys() if key.startswith(f"{session_id}:")]
            for key in keys_to_remove:
                del self.session_states[key]
            logger.info(f"Reset all progressive states for session {session_id}")
    
    def get_progress_info(self, session_id: str, agent_id: str, current_word_count: int) -> Dict:
        """Get progress information for frontend display"""
        state_key = self._get_state_key(session_id, agent_id)
        state = self.session_states.get(state_key)
        
        if not state:
            return {
                'progress_percent': 0,
                'words_until_next': 0,
                'current_interval': 0,
                'total_summaries': 0
            }
        
        words_since_last = current_word_count - state.last_summary_at
        progress_percent = min(100, (words_since_last / state.current_interval) * 100)
        words_until_next = max(0, state.current_interval - words_since_last)
        
        return {
            'progress_percent': round(progress_percent, 1),
            'words_until_next': words_until_next,
            'current_interval': state.current_interval,
            'total_summaries': state.total_summaries,
            'words_since_last': words_since_last
        }
    
    def cleanup_session(self, session_id: str):
        """Clean up all state for a completed session"""
        self.reset_session(session_id)
        logger.info(f"Cleaned up progressive interval state for session {session_id}")

# Global instance
progressive_interval_manager = ProgressiveIntervalManager()