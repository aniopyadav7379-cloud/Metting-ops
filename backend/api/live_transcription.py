"""
Live Transcription API - Time-Shift Recording Controls
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict, List
import logging
import time

from services.live_transcription_service import live_transcription_service
from auth.dependencies import get_current_user

router = APIRouter(prefix="/api/live-transcription", tags=["live-transcription"])
logger = logging.getLogger(__name__)

class LiveTranscriptionConfig(BaseModel):
    enabled: bool
    display_enabled: bool = True
    buffer_enabled: bool = True
    buffer_duration_minutes: int = 60

class RetroactiveRequest(BaseModel):
    since_minutes: int
    title: Optional[str] = None

@router.get("/status")
async def get_live_transcription_status():
    """Get current live transcription status"""
    return live_transcription_service.get_status()

@router.post("/start")
async def start_live_transcription(current_user = Depends(get_current_user)):
    """Start live transcription service (admin only)"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="superuser required")
    
    try:
        await live_transcription_service.start()
        return {"message": "Live transcription started", "status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop")
async def stop_live_transcription(current_user = Depends(get_current_user)):
    """Stop live transcription service (admin only)"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="superuser required")
    
    await live_transcription_service.stop()
    return {"message": "Live transcription stopped", "status": "success"}

@router.post("/configure")
async def configure_live_transcription(
    config: LiveTranscriptionConfig,
    current_user = Depends(get_current_user)
):
    """Configure live transcription settings (admin only)"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="superuser required")
    
    # Apply configuration
    live_transcription_service.configure(
        display_enabled=config.display_enabled,
        buffer_enabled=config.buffer_enabled,
        buffer_duration_minutes=config.buffer_duration_minutes
    )
    
    # Start/stop based on enabled flag
    if config.enabled and not live_transcription_service.is_enabled:
        await live_transcription_service.start()
    elif not config.enabled and live_transcription_service.is_enabled:
        await live_transcription_service.stop()
    
    return {
        "message": "Configuration updated",
        "status": live_transcription_service.get_status()
    }

@router.post("/create-retroactive-session")
async def create_retroactive_session(
    request: RetroactiveRequest,
    current_user = Depends(get_current_user)
):
    """Create a recording session from buffered data (TIME-SHIFT MAGIC!)"""
    try:
        session_data = await live_transcription_service.create_retroactive_session(
            since_minutes=request.since_minutes,
            title=request.title or f"Time-Shift Recording - Last {request.since_minutes} minutes"
        )
        return session_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.websocket("/ws")
async def live_transcription_websocket(websocket: WebSocket):
    """WebSocket for live transcription updates"""
    from auth.ws_auth import enforce_ws_auth

    ok, _user = await enforce_ws_auth(websocket)
    if not ok:
        return
    await websocket.accept()
    
    try:
        # Add to service connections
        live_transcription_service.add_websocket(websocket)
        
        # Send initial status
        await websocket.send_json({
            "type": "status",
            "data": live_transcription_service.get_status()
        })
        
        # Keep connection alive and handle messages
        while True:
            try:
                message = await websocket.receive_json()
                
                if message.get("type") == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": str(time.time())
                    })
                    
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"WebSocket message error: {e}")
                
    except WebSocketDisconnect:
        pass
    finally:
        # Remove from service connections
        live_transcription_service.remove_websocket(websocket)

@router.get("/buffer-preview/{minutes}")
async def get_buffer_preview(
    minutes: int,
    current_user = Depends(get_current_user)
):
    """Preview what's in the buffer for the last N minutes"""
    if minutes > live_transcription_service.buffer_duration_minutes:
        raise HTTPException(
            status_code=400, 
            detail=f"Requested minutes ({minutes}) exceeds buffer duration ({live_transcription_service.buffer_duration_minutes})"
        )
    
    segments = live_transcription_service.buffer.get_segments_since(minutes)
    
    return {
        "minutes_requested": minutes,
        "segments_found": len(segments),
        "preview": [
            {
                "text": seg.text,
                "timestamp": seg.timestamp.isoformat(),
                "speaker": seg.speaker
            } for seg in segments[-10:]  # Last 10 segments as preview
        ],
        "total_segments": len(segments)
    }
