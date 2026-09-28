"""
WebSocket Registry Service
Manages WebSocket broadcast functions to avoid circular imports
"""
import logging
from typing import Dict, Callable, Optional, Any

logger = logging.getLogger(__name__)

class WebSocketRegistry:
    """Central registry for WebSocket broadcast functions"""
    
    def __init__(self):
        self.broadcasters: Dict[str, Callable] = {}
    
    def register_broadcaster(self, name: str, func: Callable) -> None:
        """Register a broadcast function"""
        self.broadcasters[name] = func
        logger.info(f"Registered WebSocket broadcaster: {name}")
    
    def unregister_broadcaster(self, name: str) -> None:
        """Unregister a broadcast function"""
        if name in self.broadcasters:
            del self.broadcasters[name]
            logger.info(f"Unregistered WebSocket broadcaster: {name}")
    
    def get_broadcaster(self, name: str) -> Optional[Callable]:
        """Get a registered broadcast function"""
        return self.broadcasters.get(name)
    
    async def broadcast_to_all(self, session_id: str, segment: Dict[str, Any]) -> None:
        """Broadcast to all registered WebSocket endpoints"""
        for name, broadcaster in self.broadcasters.items():
            try:
                await broadcaster(session_id, segment)
                logger.debug(f"Broadcast to {name} for session {session_id}")
            except Exception as e:
                logger.error(f"Error broadcasting to {name}: {e}")

# Global instance
websocket_registry = WebSocketRegistry()