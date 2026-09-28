#!/usr/bin/env python3
"""
Real-time Audio Level Monitoring
Provides audio level data for visualization
"""

import asyncio
import numpy as np
import pyaudio
import logging
from typing import Optional, Dict, Any
import json
from datetime import datetime

logger = logging.getLogger(__name__)

class AudioLevelMonitor:
    """Monitor audio levels in real-time"""
    
    def __init__(self, device_index: Optional[int] = None, simulate: bool = False):
        self.device_index = device_index
        self.sample_rate = 48000  # Updated to match USB microphone
        self.chunk_size = 1024
        self.channels = 1
        self.format = pyaudio.paInt16
        self.audio = None
        self.stream = None
        self.monitoring = False
        self.simulate = simulate
        self.simulation_time = 0
        
    def start_monitoring(self) -> bool:
        """Start audio monitoring"""
        if self.simulate:
            logger.info("📊 Starting simulated audio monitor")
            self.monitoring = True
            self.simulation_time = 0
            return True
            
        try:
            self.audio = pyaudio.PyAudio()
            
            # Get device info
            if self.device_index is None:
                # Use default input device
                device_info = self.audio.get_default_input_device_info()
                self.device_index = device_info['index']
            else:
                device_info = self.audio.get_device_info_by_index(self.device_index)
            
            logger.info(f"📊 Starting audio monitor on device: {device_info['name']}")
            
            # Open stream with flexible sample rate
            try:
                self.stream = self.audio.open(
                    format=self.format,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=self.chunk_size
                )
            except Exception as e:
                # Try with device's default sample rate
                self.sample_rate = int(device_info['defaultSampleRate'])
                logger.info(f"   Retrying with sample rate: {self.sample_rate}")
                self.stream = self.audio.open(
                    format=self.format,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=self.chunk_size
                )
            
            self.monitoring = True
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to start audio monitoring: {e}")
            logger.info("   Falling back to simulation mode")
            self.simulate = True
            self.monitoring = True
            return True
    
    def stop_monitoring(self):
        """Stop audio monitoring"""
        self.monitoring = False
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            
        if self.audio:
            self.audio.terminate()
            
        logger.info("📊 Audio monitoring stopped")
    
    def get_audio_level(self) -> Dict[str, Any]:
        """Get current audio level"""
        if not self.monitoring:
            return {
                "level": 0,
                "db": -60,
                "peak": 0,
                "status": "not_monitoring"
            }
        
        if self.simulate:
            # Generate simulated audio levels
            import math
            self.simulation_time += 0.05
            
            # Simulate speech pattern
            base_level = 0.2
            speech_pattern = abs(math.sin(self.simulation_time * 0.5)) * 0.3
            noise = np.random.random() * 0.1
            
            level = base_level + speech_pattern + noise
            peak = level + np.random.random() * 0.1
            
            # Add occasional spikes
            if np.random.random() > 0.95:
                level = 0.8
                peak = 0.9
            
            # Convert to dB
            db = 20 * np.log10(level) if level > 0 else -60
            
            return {
                "level": float(level),
                "db": float(db),
                "peak": float(peak),
                "timestamp": datetime.now().isoformat(),
                "status": "simulated"
            }
        
        if not self.stream:
            return {
                "level": 0,
                "db": -60,
                "peak": 0,
                "status": "no_stream"
            }
        
        try:
            # Read audio chunk
            data = self.stream.read(self.chunk_size, exception_on_overflow=False)
            
            # Convert to numpy array
            audio_data = np.frombuffer(data, dtype=np.int16)
            
            # Calculate RMS (Root Mean Square) level
            rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            
            # Normalize to 0-1 range
            max_value = 32768.0  # Max value for 16-bit audio
            normalized_rms = rms / max_value
            
            # Calculate peak level
            peak = np.max(np.abs(audio_data)) / max_value
            
            # Convert to decibels
            db = 20 * np.log10(normalized_rms) if normalized_rms > 0 else -60
            
            # Clamp values
            db = max(-60, min(0, db))
            normalized_rms = max(0, min(1, normalized_rms))
            peak = max(0, min(1, peak))
            
            return {
                "level": float(normalized_rms),
                "db": float(db),
                "peak": float(peak),
                "timestamp": datetime.now().isoformat(),
                "status": "monitoring"
            }
            
        except Exception as e:
            logger.error(f"❌ Error reading audio level: {e}")
            return {
                "level": 0,
                "db": -60,
                "peak": 0,
                "status": "error",
                "error": str(e)
            }
    
    async def stream_levels(self, interval: float = 0.1) -> Any:
        """Stream audio levels at specified interval"""
        while self.monitoring:
            yield self.get_audio_level()
            await asyncio.sleep(interval)


# WebSocket handler for audio levels
async def audio_level_websocket_handler(websocket):
    """Handle WebSocket connection for audio level streaming"""
    monitor = AudioLevelMonitor()
    
    try:
        # Start monitoring
        if not monitor.start_monitoring():
            await websocket.send_json({
                "type": "error",
                "message": "Failed to start audio monitoring"
            })
            return
        
        # Send initial status
        await websocket.send_json({
            "type": "status",
            "message": "Audio monitoring started"
        })
        
        # Stream audio levels
        async for level_data in monitor.stream_levels(interval=0.05):  # 20 updates per second
            await websocket.send_json({
                "type": "audio_level",
                **level_data
            })
            
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await websocket.send_json({
            "type": "error",
            "message": str(e)
        })
    finally:
        monitor.stop_monitoring()


# Visualization data generator for testing
def generate_visualization_data():
    """Generate sample visualization data"""
    import math
    
    # Frequency spectrum (simulated)
    frequencies = np.logspace(1, 4, 32)  # 10Hz to 10kHz, 32 bands
    spectrum = []
    
    for i, freq in enumerate(frequencies):
        # Simulate frequency response
        magnitude = np.random.random() * 0.5 + 0.2
        if 200 <= freq <= 4000:  # Boost speech frequencies
            magnitude *= 1.5
        
        spectrum.append({
            "frequency": float(freq),
            "magnitude": float(magnitude),
            "db": float(20 * np.log10(magnitude))
        })
    
    # Waveform data (last 100ms)
    sample_count = 1600  # 100ms at 16kHz
    time_axis = np.linspace(0, 0.1, sample_count)
    waveform = np.sin(2 * np.pi * 440 * time_axis) * 0.3  # 440Hz tone
    waveform += np.random.normal(0, 0.05, sample_count)  # Add noise
    
    return {
        "spectrum": spectrum,
        "waveform": waveform.tolist(),
        "sample_rate": 16000,
        "visualization_type": "realtime"
    }


if __name__ == "__main__":
    # Test audio level monitoring
    print("🎤 Testing Audio Level Monitor")
    print("=" * 50)
    
    # Try real monitoring first, will fall back to simulation if needed
    monitor = AudioLevelMonitor()
    
    if monitor.start_monitoring():
        status = "simulated" if monitor.simulate else "real"
        print(f"✅ Monitoring started ({status})")
        
        # Monitor for 5 seconds
        import time
        for i in range(50):
            level = monitor.get_audio_level()
            bar_length = int(level["level"] * 50)
            bar = "█" * bar_length + "░" * (50 - bar_length)
            status_icon = "🎤" if level["status"] == "monitoring" else "🎭"
            print(f"\r{status_icon} [{bar}] {level['db']:.1f} dB", end="", flush=True)
            time.sleep(0.1)
        
        print("\n✅ Test complete")
        monitor.stop_monitoring()
    else:
        print("❌ Failed to start monitoring")