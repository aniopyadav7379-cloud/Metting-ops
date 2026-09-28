"""Simple audio level monitoring for USB microphone"""
import asyncio
import json
import numpy as np
from fastapi import WebSocket
import sounddevice as sd
import logging

logger = logging.getLogger(__name__)

class SimpleAudioMonitor:
    def __init__(self):
        self.current_level = 0.0
        self.current_db = -60.0
        self.stream = None
        self.monitoring = False
        self.peak_level = 0.0
        self.peak_decay_rate = 0.95  # Peak decay per update
        
    def audio_callback(self, indata, frames, time, status):
        """Process audio and calculate level"""
        if status:
            logger.warning(f"Audio callback status: {status}")
        
        # Calculate RMS level
        audio_data = indata[:, 0] if len(indata.shape) > 1 else indata
        
        # Apply input gain reduction to prevent clipping
        audio_data = audio_data * 0.3  # Reduce gain by 70%
        
        rms = np.sqrt(np.mean(audio_data**2))
        
        # Convert to dB with proper reference
        db = 20 * np.log10(max(rms, 1e-10))
        
        # Normalize level to 0-1 range with headroom
        # Map -60dB to 0dB to 0.0 to 1.0
        normalized_level = max(0, min(1, (db + 60) / 60))
        
        # Store values
        self.current_level = float(normalized_level)
        self.current_db = float(db)
        
        # Update peak with decay
        if normalized_level > self.peak_level:
            self.peak_level = normalized_level
        else:
            self.peak_level *= self.peak_decay_rate
        
    def start(self):
        """Start monitoring audio levels"""
        try:
            # List available devices
            devices = sd.query_devices()
            logger.info("Available audio devices:")
            for i, dev in enumerate(devices):
                if dev['max_input_channels'] > 0:
                    logger.info(f"  {i}: {dev['name']} ({dev['max_input_channels']} ch)")
            
            # Try to find USB microphone or use default
            usb_device = None
            for i, dev in enumerate(devices):
                if dev['max_input_channels'] > 0:
                    name_lower = dev['name'].lower()
                    if any(keyword in name_lower for keyword in ['usb', 'device', 'pnp']):
                        usb_device = i
                        logger.info(f"Found USB/PnP device: {i}: {dev['name']}")
                        break
            
            # Use default device if USB not found (default often maps to the right device)
            if usb_device is None:
                logger.info("No USB device found, using default input device")
            
            # Start audio stream
            self.stream = sd.InputStream(
                device=usb_device,  # Use USB if found, otherwise default (None)
                channels=1,
                samplerate=44100,
                callback=self.audio_callback,
                blocksize=1024
            )
            self.stream.start()
            self.monitoring = True
            logger.info(f"Started audio monitoring on device: {self.stream.device}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start audio monitoring: {e}")
            return False
    
    def stop(self):
        """Stop monitoring"""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.monitoring = False
        
    def get_level(self):
        """Get current audio level"""
        return {
            "type": "audio_level",
            "level": float(self.current_level),
            "db": float(self.current_db),
            "peak": float(self.peak_level)
        }

# Global monitor instance
audio_monitor = SimpleAudioMonitor()

async def handle_audio_levels_websocket(websocket: WebSocket):
    """Handle WebSocket connection for audio levels"""
    # WebSocket is already accepted in main.py - DO NOT accept again
    
    # Start monitoring if not already running
    if not audio_monitor.monitoring:
        if not audio_monitor.start():
            await websocket.send_json({
                "type": "error",
                "message": "Failed to start audio monitoring"
            })
            await websocket.close()
            return
    
    try:
        while True:
            # Send current level
            level_data = audio_monitor.get_level()
            await websocket.send_json(level_data)
            
            # Wait a bit before next update
            await asyncio.sleep(0.1)  # 10 updates per second for smoother UI
            
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Don't stop monitoring - other clients might be connected
        pass