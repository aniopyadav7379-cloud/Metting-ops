from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import json
import logging
import os

# Try to import pyaudio, but handle gracefully if not available
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"PyAudio not available: {e}. Audio device enumeration will be disabled.")
    PYAUDIO_AVAILABLE = False
    pyaudio = None

from database.models import Settings
from database.database import get_db

logger = logging.getLogger(__name__)


_SENSITIVE_SETTING_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


def _redact_setting_value_for_log(key: str, value: Any) -> Any:
    """Keep arbitrary admin-managed setting values out of application logs.

    Settings are extensible, so an integration credential can be stored under
    a key unknown to this module.  The value is still stored and returned to
    the authorized settings workflow; only diagnostic logs are redacted.
    """
    normalized_key = str(key).lower().replace("-", "_")
    if any(part in normalized_key for part in _SENSITIVE_SETTING_KEY_PARTS):
        return "[REDACTED]"
    return value

class SettingsManager:
    """Manages application settings and configuration"""
    
    # Default settings structure
    DEFAULT_SETTINGS = {
        "audio": {
            "input_source": os.getenv("AUDIO_DEVICE", "usb"),
            "sample_rate": int(os.getenv("AUDIO_SAMPLE_RATE", "44100")),
            "channels": int(os.getenv("AUDIO_CHANNELS", "1")),
            "input_gain": 70,
            "noise_reduction": True,
            "auto_gain_control": True
        },
        "ai": {
            "npu_acceleration": True,
            "speaker_diarization": True,
            "real_time_processing": True,
            "whisper_model": "whisperx-npu",
            "llm_model": "gpt-oss-20b",
            "language": "auto",
            "max_speakers": 10
        },
        "storage": {
            "auto_cleanup_days": 30,
            "export_format": "json",
            "recording_format": "wav",
            "compression": False,
            "max_storage_gb": 500
        },
        "network": {
            "remote_access": True,
            "api_port": 9050,
            "websocket_port": 9050,
            "ssl_enabled": False,
            "allowed_origins": ["*"]
        }
    }
    
    def __init__(self):
        self.cached_settings = {}
        self._initialize_defaults()
    
    def _initialize_defaults(self):
        """Initialize default settings if they don't exist"""
        db = next(get_db())
        try:
            for category, settings in self.DEFAULT_SETTINGS.items():
                for key, value in settings.items():
                    full_key = f"{category}.{key}"
                    existing = db.query(Settings).filter(Settings.key == full_key).first()
                    
                    if not existing:
                        # Convert all values to strings for storage
                        if isinstance(value, list):
                            stored_value = json.dumps(value)
                        elif isinstance(value, bool):
                            stored_value = str(value).lower()  # "true" or "false"
                        elif isinstance(value, (int, float)):
                            stored_value = str(value)
                        else:
                            stored_value = str(value)

                        setting = Settings(
                            key=full_key,
                            value=stored_value,
                            category=category,
                            description=self._get_setting_description(full_key)
                        )
                        db.add(setting)
            
            db.commit()
            logger.info("✅ Default settings initialized")
        except Exception as e:
            logger.error(f"Error initializing default settings: {e}")
            db.rollback()
        finally:
            db.close()
    
    def _get_setting_description(self, key: str) -> str:
        """Get description for a setting key"""
        descriptions = {
            "audio.input_source": "Audio input device (default, line_in, usb, bluetooth)",
            "audio.sample_rate": "Audio sampling rate in Hz",
            "audio.channels": "Number of audio channels (1=mono, 2=stereo)",
            "audio.input_gain": "Input gain level (0-100)",
            "audio.noise_reduction": "Enable noise reduction processing",
            "audio.auto_gain_control": "Enable automatic gain control",
            "ai.npu_acceleration": "Use NPU for AI acceleration when available",
            "ai.speaker_diarization": "Enable speaker identification",
            "ai.real_time_processing": "Process audio in real-time",
            "ai.whisper_model": "Whisper model size (tiny, base, small, medium, large)",
            "ai.llm_model": "Active LLM model for AI tasks (gpt-oss-20b or granite-3.3-2b-instruct)",
            "ai.language": "Language for transcription (auto for automatic detection)",
            "ai.max_speakers": "Maximum number of speakers to detect",
            "storage.auto_cleanup_days": "Delete old recordings after N days (0 = never)",
            "storage.export_format": "Default export format (json, txt, pdf)",
            "storage.recording_format": "Audio recording format (wav, mp3, flac)",
            "storage.compression": "Enable audio compression",
            "storage.max_storage_gb": "Maximum storage space in GB",
            "network.remote_access": "Enable remote web access",
            "network.api_port": "API server port",
            "network.websocket_port": "WebSocket server port",
            "network.ssl_enabled": "Enable SSL/TLS encryption",
            "network.allowed_origins": "Allowed CORS origins"
        }
        return descriptions.get(key, "")
    
    def get_all_settings(self) -> Dict[str, Any]:
        """Get all settings organized by category"""
        db = next(get_db())
        try:
            settings = db.query(Settings).all()
            result = {}
            
            for setting in settings:
                if setting.category not in result:
                    result[setting.category] = {}
                
                # Extract the key name without category prefix
                key_name = setting.key.split('.', 1)[1] if '.' in setting.key else setting.key
                
                # Special handling for list values (e.g., allowed_origins)
                display_value = setting.value
                if setting.key == "network.allowed_origins" and isinstance(setting.value, str):
                    try:
                        display_value = json.loads(setting.value)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Could not decode JSON setting %s",
                            setting.key,
                        )
                        display_value = [] # Default to empty list on error

                result[setting.category][key_name] = {
                    "value": display_value,
                    "description": setting.description,
                    "updated_at": setting.updated_at.isoformat() if setting.updated_at else None
                }
            
            return result
        finally:
            db.close()
    
    def get_setting(self, key: str) -> Any:
        """Get a specific setting value"""
        db = next(get_db())
        try:
            setting = db.query(Settings).filter(Settings.key == key).first()
            if setting:
                return setting.value
            
            # Return default if not found
            if '.' in key:
                category, setting_key = key.split('.', 1)
                return self.DEFAULT_SETTINGS.get(category, {}).get(setting_key)
            
            return None
        finally:
            db.close()
    
    def update_setting(self, key: str, value: Any) -> bool:
        """Update a specific setting"""
        db = next(get_db())
        try:
            setting = db.query(Settings).filter(Settings.key == key).first()
            
            # Serialize list values to JSON string
            stored_value = json.dumps(value) if isinstance(value, list) else value

            if setting:
                setting.value = stored_value
                setting.updated_at = datetime.now(timezone.utc)
            else:
                # Create new setting if it doesn't exist
                category = key.split('.')[0] if '.' in key else 'general'
                setting = Settings(
                    key=key,
                    value=stored_value,
                    category=category,
                    description=self._get_setting_description(key)
                )
                db.add(setting)
            
            db.commit()
            
            # Clear cache
            if key in self.cached_settings:
                del self.cached_settings[key]
            
            logger.info(
                "Updated setting: %s = %s",
                key,
                _redact_setting_value_for_log(key, value),
            )
            return True
        except Exception as e:
            logger.error(f"Error updating setting {key}: {e}")
            db.rollback()
            return False
        finally:
            db.close()
    
    def update_category_settings(self, category: str, settings: Dict[str, Any]) -> bool:
        """Update all settings for a category"""
        db = next(get_db())
        try:
            for key, value in settings.items():
                full_key = f"{category}.{key}"
                setting = db.query(Settings).filter(Settings.key == full_key).first()
                
                # Serialize list values to JSON string
                stored_value = json.dumps(value) if isinstance(value, list) else value

                if setting:
                    setting.value = stored_value
                    setting.updated_at = datetime.now(timezone.utc)
                else:
                    setting = Settings(
                        key=full_key,
                        value=stored_value,
                        category=category,
                        description=self._get_setting_description(full_key)
                    )
                    db.add(setting)
            
            db.commit()
            logger.info(f"📝 Updated {len(settings)} settings in category: {category}")
            return True
        except Exception as e:
            logger.error(f"Error updating category settings: {e}")
            db.rollback()
            return False
        finally:
            db.close()
    
    def get_audio_devices(self) -> List[Dict[str, Any]]:
        """Get list of available audio input devices"""
        devices = []
        
        try:
            p = pyaudio.PyAudio()
            
            # Add default device option
            devices.append({
                "id": "default",
                "name": "System Default",
                "channels": 2,
                "sample_rate": 16000
            })
            
            # Get all input devices
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info['maxInputChannels'] > 0:  # Input device
                    devices.append({
                        "id": f"device_{i}",
                        "name": info['name'],
                        "channels": info['maxInputChannels'],
                        "sample_rate": int(info['defaultSampleRate'])
                    })
            
            p.terminate()
            
            # Add mock devices for hardware inputs
            devices.extend([
                {
                    "id": "line_in",
                    "name": "Line In (XLR)",
                    "channels": 2,
                    "sample_rate": 48000
                },
                {
                    "id": "usb",
                    "name": "USB Microphone",
                    "channels": 1,
                    "sample_rate": 44100
                },
                {
                    "id": "bluetooth",
                    "name": "Bluetooth Audio",
                    "channels": 1,
                    "sample_rate": 16000
                }
            ])
            
        except Exception as e:
            logger.error(f"Error enumerating audio devices: {e}")
            # Return mock devices if PyAudio fails
            devices = [
                {"id": "default", "name": "System Default", "channels": 2, "sample_rate": 16000},
                {"id": "line_in", "name": "Line In (XLR)", "channels": 2, "sample_rate": 48000},
                {"id": "usb", "name": "USB Microphone", "channels": 1, "sample_rate": 44100},
                {"id": "bluetooth", "name": "Bluetooth Audio", "channels": 1, "sample_rate": 16000}
            ]
        
        return devices
    
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information"""
        import platform
        import psutil
        
        return {
            "platform": platform.system(),
            "platform_version": platform.version(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
            "memory_available_gb": round(psutil.virtual_memory().available / (1024**3), 2),
            "disk_total_gb": round(psutil.disk_usage('/').total / (1024**3), 2),
            "disk_free_gb": round(psutil.disk_usage('/').free / (1024**3), 2)
        }
    
    def export_settings(self) -> Dict[str, Any]:
        """Export all settings for backup"""
        return {
            "settings": self.get_all_settings(),
            "export_date": datetime.now(timezone.utc).isoformat(),
            "version": "1.0"
        }
    
    def import_settings(self, settings_data: Dict[str, Any]) -> bool:
        """Import settings from backup"""
        try:
            settings = settings_data.get("settings", {})
            
            for category, category_settings in settings.items():
                update_dict = {}
                for key, setting_info in category_settings.items():
                    if isinstance(setting_info, dict) and 'value' in setting_info:
                        update_dict[key] = setting_info['value']
                    else:
                        update_dict[key] = setting_info
                
                self.update_category_settings(category, update_dict)
            
            logger.info("✅ Settings imported successfully")
            return True
        except Exception as e:
            logger.error(f"Error importing settings: {e}")
            return False

# Global settings manager instance
settings_manager = SettingsManager()
