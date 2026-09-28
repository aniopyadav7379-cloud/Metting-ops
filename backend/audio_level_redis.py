#!/usr/bin/env python3
"""
Redis-based Audio Level Broadcasting
Uses Redis pub/sub for efficient WebSocket distribution
"""

import asyncio
import os
import redis.asyncio as redis
import json
import logging
from datetime import datetime
from typing import Optional
import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6381")

class AudioLevelRedisService:
    """Service to publish audio levels to Redis"""

    def __init__(self, redis_url: str = _DEFAULT_REDIS_URL):
        self.redis_url = redis_url
        self.redis_client: Optional[redis.Redis] = None
        self.channel = "audio_levels"
        self.running = False
        
    async def connect(self):
        """Connect to Redis"""
        try:
            self.redis_client = await redis.from_url(self.redis_url, decode_responses=True)
            await self.redis_client.ping()
            logger.info("✅ Connected to Redis for audio levels")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return False
            
    async def publish_audio_level(self, level: float, peak: float, db: float):
        """Publish audio level to Redis channel"""
        if not self.redis_client:
            return
            
        data = {
            "type": "audio_level",
            "level": level,
            "peak": peak,
            "db": db,
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            await self.redis_client.publish(self.channel, json.dumps(data))
        except Exception as e:
            logger.error(f"Failed to publish audio level: {e}")
            
    async def start_audio_monitoring(self):
        """Start monitoring audio and publishing to Redis"""
        from audio_level_direct_usb import DirectUSBAudioMonitor
        
        monitor = DirectUSBAudioMonitor()
        
        if not await monitor.start_monitoring():
            logger.error("Failed to start USB audio monitoring")
            return
            
        self.running = True
        logger.info("📊 Started USB audio monitoring with Redis (Texas Instruments PCM2902)")
        
        try:
            while self.running:
                # Get audio level from USB
                level_data = await monitor.get_audio_level()
                
                # Publish to Redis
                await self.publish_audio_level(
                    level_data.get('level', 0),
                    level_data.get('peak', 0),
                    level_data.get('db', -60)
                )
                
                # Small delay (50ms = 20 updates per second)
                await asyncio.sleep(0.05)
                
        except Exception as e:
            logger.error(f"Audio monitoring error: {e}")
        finally:
            await monitor.stop_monitoring()
            self.running = False
            
    async def stop(self):
        """Stop the service"""
        self.running = False
        if self.redis_client:
            await self.redis_client.close()


class AudioLevelWebSocketHandler:
    """Handle WebSocket connections with Redis subscription"""
    
    def __init__(self, redis_url: str = _DEFAULT_REDIS_URL):
        self.redis_url = redis_url

    async def handle_websocket(self, websocket):
        """Handle a WebSocket connection"""
        # Create Redis connection for this WebSocket
        redis_client = await redis.from_url(self.redis_url, decode_responses=True)
        pubsub = redis_client.pubsub()
        
        try:
            # Subscribe to audio levels channel
            await pubsub.subscribe("audio_levels")
            logger.info("WebSocket subscribed to audio levels")
            
            # Send initial status
            await websocket.send_json({
                "type": "status",
                "message": "Connected to audio stream",
                "timestamp": datetime.now().isoformat()
            })
            
            # Listen for messages
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    # Forward to WebSocket
                    data = message['data']
                    if isinstance(data, str):
                        await websocket.send_text(data)
                        
        except Exception as e:
            logger.error(f"WebSocket handler error: {e}")
        finally:
            await pubsub.unsubscribe("audio_levels")
            await redis_client.close()