"""Simple settings API endpoints for Meeting-Ops."""

from fastapi import APIRouter, HTTPException, Depends
from typing import List
import logging

from auth.dependencies import get_current_user
from auth.models import User
from models.system_settings import (
    AllSettings,
    AudioDevice,
    AudioVolumeRequest,
    AudioVolumeResponse,
    NetworkInfo,
    NetworkConfigRequest,
    SystemApplyRequest,
    AuthentikTestRequest,
    AuthentikTestResponse
)
from services.system_service import SystemService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["simple-settings"])


@router.get("/api/simple/settings", response_model=AllSettings)
async def get_all_settings(current_user: User = Depends(get_current_user)):
    """Load all system settings."""
    try:
        settings = SystemService.load_settings()
        return settings
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error loading settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/simple/settings", response_model=dict)
async def save_all_settings(settings: AllSettings, current_user: User = Depends(get_current_user)):
    """Save all system settings."""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        success = SystemService.save_settings(settings)
        if success:
            return {"success": True, "message": "Settings saved successfully"}
        else:
            raise HTTPException(status_code=500, detail="Failed to save settings")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/simple/audio-devices", response_model=List[AudioDevice])
async def get_audio_devices(current_user: User = Depends(get_current_user)):
    """Get list of available audio devices."""
    try:
        devices = SystemService.get_audio_devices()
        return devices
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting audio devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/simple/audio/volume", response_model=AudioVolumeResponse)
async def set_audio_volume(request: AudioVolumeRequest, current_user: User = Depends(get_current_user)):
    """Set microphone volume for a device."""
    try:
        response = SystemService.set_audio_volume(request.device, request.volume)
        if not response.success:
            raise HTTPException(status_code=500, detail="Failed to set volume")
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting audio volume: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/simple/audio/volume/{device}", response_model=dict)
async def get_audio_volume(device: str, current_user: User = Depends(get_current_user)):
    """Get current volume for a device as percentage."""
    try:
        # Replace underscores with colons (URL encoding issue)
        device = device.replace('_', ':').replace('-', ',')
        volume = SystemService.get_audio_volume(device)
        return {"device": device, "volume": volume}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting audio volume: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/simple/system/network", response_model=NetworkInfo)
async def get_network_info(current_user: User = Depends(get_current_user)):
    """Get current network configuration."""
    try:
        network_info = SystemService.get_network_info()
        return network_info
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting network info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/simple/system/network", response_model=dict)
async def update_network_settings(request: NetworkConfigRequest, current_user: User = Depends(get_current_user)):
    """Update network configuration settings."""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        # Convert to dict and filter out None values
        config = {k: v for k, v in request.dict().items() if v is not None}
        
        success = await SystemService.update_network_config(config)
        
        if success:
            return {"success": True, "message": "Network settings updated"}
        else:
            # Note: Some operations require sudo
            return {
                "success": True,
                "message": "Settings saved (some changes require sudo to apply)",
                "requiresSudo": True
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating network settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/simple/system/apply", response_model=dict)
async def apply_system_settings(request: SystemApplyRequest, current_user: User = Depends(get_current_user)):
    """Apply WiFi, NPU, and performance settings."""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        config = request.dict()
        success = await SystemService.apply_system_settings(config)
        
        if success:
            return {
                "success": True,
                "message": "System settings applied",
                "note": "Some changes may require restart"
            }
        else:
            return {
                "success": True,
                "message": "Settings saved (some changes require sudo to apply)",
                "requiresSudo": True
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying system settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/simple/auth/test-authentik", response_model=AuthentikTestResponse)
async def test_authentik_connection(request: AuthentikTestRequest, current_user: User = Depends(get_current_user)):
    """Test connection to Authentik server."""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        response = await SystemService.test_authentik_connection(
            request.authentikUrl,
            request.clientId,
            request.clientSecret,
            request.redirectUri
        )
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing Authentik connection: {e}")
        return AuthentikTestResponse(
            success=False,
            message=f"Error: {str(e)}",
            details=None
        )


# Additional helper endpoints

@router.get("/api/simple/settings/recording", response_model=dict)
async def get_recording_settings(current_user: User = Depends(get_current_user)):
    """Get just recording-related settings."""
    try:
        settings = SystemService.load_settings()
        return {
            "recordingQuality": settings.recordingQuality,
            "defaultMicrophone": settings.defaultMicrophone,
            "microphoneVolume": settings.microphoneVolume,
            "enableNoiseReduction": settings.enableNoiseReduction,
            "enableAutoGainControl": settings.enableAutoGainControl
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting recording settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/simple/settings/transcription", response_model=dict)
async def get_transcription_settings(current_user: User = Depends(get_current_user)):
    """Get just transcription-related settings."""
    try:
        settings = SystemService.load_settings()
        return {
            "transcriptionModel": settings.transcriptionModel,
            "transcriptionLanguage": settings.transcriptionLanguage,
            "enableSpeakerDiarization": settings.enableSpeakerDiarization,
            "maxSpeakers": settings.maxSpeakers,
            "enableVAD": settings.enableVAD,
            "vadThreshold": settings.vadThreshold
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting transcription settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/simple/settings/ai", response_model=dict)
async def get_ai_settings(current_user: User = Depends(get_current_user)):
    """Get just AI-related settings."""
    try:
        settings = SystemService.load_settings()
        return {
            "enableLiveAI": settings.enableLiveAI,
            "aiProvider": settings.aiProvider,
            "aiModel": settings.aiModel,
            "aiTemperature": settings.aiTemperature,
            "enableAutoSummary": settings.enableAutoSummary,
            "enableLiveSummarization": settings.enableLiveSummarization,
            "summaryUpdateInterval": settings.summaryUpdateInterval
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting AI settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/simple/settings/reset", response_model=dict)
async def reset_settings_to_defaults(current_user: User = Depends(get_current_user)):
    """Reset all settings to defaults."""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        default_settings = AllSettings()
        success = SystemService.save_settings(default_settings)
        
        if success:
            return {"success": True, "message": "Settings reset to defaults"}
        else:
            raise HTTPException(status_code=500, detail="Failed to reset settings")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resetting settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/simple/system/info", response_model=dict)
async def get_system_info(current_user: User = Depends(get_current_user)):
    """Get system information summary."""
    try:
        import platform
        import psutil
        
        # Get system info
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        return {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
            "processor": platform.processor(),
            "cpu_count": psutil.cpu_count(),
            "cpu_percent": cpu_percent,
            "memory_total_gb": round(memory.total / (1024**3), 2),
            "memory_used_gb": round(memory.used / (1024**3), 2),
            "memory_percent": memory.percent,
            "disk_total_gb": round(disk.total / (1024**3), 2),
            "disk_used_gb": round(disk.used / (1024**3), 2),
            "disk_percent": disk.percent
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        raise HTTPException(status_code=500, detail=str(e))
