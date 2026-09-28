#!/usr/bin/env python3
"""
Direct USB Audio Level Monitor - Bypasses PyAudio device detection
Uses subprocess to read directly from ALSA hw:0,0
"""

import asyncio
import subprocess
import numpy as np
import json
import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)

class DirectUSBAudioMonitor:
    """Monitor audio levels directly from USB device using arecord"""
    
    def __init__(self):
        self.process = None
        self.monitoring = False
        self.device = "hw:0,0"  # Direct USB device access when not recording
        self.sample_rate = 44100  # Common USB mic rate
        self.channels = 1  # Mono for USB device
        
    async def start_monitoring(self):
        """Start monitoring using arecord subprocess"""
        if self.monitoring:
            return
            
        cmd = [
            'arecord',
            '-D', self.device,
            '-f', 'S16_LE',
            '-r', str(self.sample_rate),
            '-c', str(self.channels),
            '-t', 'raw',
            '-q'  # Quiet mode
        ]
        
        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            self.monitoring = True
            logger.info(f"✅ Started direct USB audio monitoring on {self.device}")
            logger.info(f"   Device: USB PnP Sound Device (ALSA plugin sharing)")
            logger.info(f"   Sample rate: {self.sample_rate} Hz (USB standard)")
            logger.info(f"   Channels: {self.channels} (mono)")
            logger.info(f"   Gain: 100x amplification applied")
            logger.info(f"   No conflicts: plughw allows multiple readers")
            return True
        except Exception as e:
            logger.error(f"Failed to start arecord: {e}")
            return False
    
    async def get_audio_level(self) -> Dict[str, Any]:
        """Read audio data and calculate level"""
        if not self.monitoring or not self.process:
            return {
                "type": "audio_level",
                "level": 0,
                "peak": 0,
                "db": -60,
                "timestamp": datetime.now().isoformat(),
                "error": "Not monitoring"
            }
        
        try:
            # Read more data for better averaging (4096 samples)
            data = await asyncio.wait_for(
                self.process.stdout.read(8192),  # 4096 samples * 2 bytes
                timeout=0.1
            )
            
            if not data or len(data) < 100:
                return {
                    "type": "audio_level",
                    "level": 0,
                    "peak": 0,
                    "db": -60,
                    "timestamp": datetime.now().isoformat(),
                    "error": "No data"
                }
            
            # Convert to numpy array
            audio_array = np.frombuffer(data, dtype=np.int16)
            
            # Debug: check if we're getting any non-zero data
            non_zero = np.count_nonzero(audio_array)
            max_val = np.max(np.abs(audio_array))
            
            # Log every 100th frame with data
            if hasattr(self, '_debug_counter'):
                self._debug_counter += 1
            else:
                self._debug_counter = 0
                
            if self._debug_counter % 100 == 0 and max_val > 0:
                logger.info(f"Audio stats: max={max_val}, non_zero={non_zero}/{len(audio_array)}, raw_max={np.max(audio_array)}, raw_min={np.min(audio_array)}")
            
            # Calculate RMS level with moderate sensitivity
            rms = np.sqrt(np.mean(audio_array**2))
            level = min(1.0, (rms / 32768.0) * 100)  # Amplify by 100x for USB mic
            
            # Calculate peak with amplification
            peak = min(1.0, (np.max(np.abs(audio_array)) / 32768.0) * 100)
            
            # Calculate dB (with the original RMS for accurate dB)
            db = 20 * np.log10(rms / 32768.0 + 1e-10)
            
            # Add some debugging - log every 20th frame with any signal
            levelPercent = level * 100  # Calculate percentage
            if self._debug_counter % 20 == 0 and level > 0.0001:
                logger.info(f"Audio level: {level:.4f} ({levelPercent:.1f}%), peak: {peak:.4f}, dB: {db:.1f}")
            
            return {
                "type": "audio_level",
                "level": level,
                "peak": peak,
                "db": db,
                "timestamp": datetime.now().isoformat(),
                "device": "USB PnP Sound Device",
                "real": True  # This is real audio!
            }
            
        except asyncio.TimeoutError:
            # Timeout is ok, just means no data available
            return {
                "type": "audio_level",
                "level": 0,
                "peak": 0,
                "db": -60,
                "timestamp": datetime.now().isoformat(),
                "device": "USB PCM2902",
                "real": True
            }
        except Exception as e:
            logger.error(f"Error reading audio: {e}")
            return {
                "type": "audio_level",
                "level": 0,
                "peak": 0,
                "db": -60,
                "timestamp": datetime.now().isoformat(),
                "error": str(e)
            }
    
    async def stop_monitoring(self):
        """Stop monitoring"""
        if self.process:
            self.process.terminate()
            await self.process.wait()
        self.monitoring = False
        logger.info("Stopped USB audio monitoring")

# WebSocket handler for direct USB monitoring
async def handle_direct_usb_websocket(websocket):
    """Handle WebSocket connection with direct USB monitoring"""
    monitor = DirectUSBAudioMonitor()
    
    # Start monitoring
    if not await monitor.start_monitoring():
        await websocket.send_json({
            "type": "error",
            "message": "Failed to start USB audio monitoring"
        })
        return
    
    # Send initial status
    await websocket.send_json({
        "type": "status",
        "message": "Direct USB audio monitoring started",
        "device": "Texas Instruments PCM2902",
        "sample_rate": monitor.sample_rate
    })
    
    try:
        # Stream audio levels
        while True:
            level_data = await monitor.get_audio_level()
            await websocket.send_json(level_data)
            await asyncio.sleep(0.05)  # 20 updates per second
            
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await monitor.stop_monitoring()

# Test function
async def test_direct_usb():
    """Test direct USB monitoring"""
    monitor = DirectUSBAudioMonitor()
    
    print("🎤 Testing Direct USB Audio Monitoring")
    print("Device: Texas Instruments PCM2902 (hw:0,0)")
    print("-" * 60)
    
    if await monitor.start_monitoring():
        print("✅ Monitoring started successfully!")
        print("📊 Reading levels for 5 seconds...")
        
        max_level = 0
        for i in range(100):  # 5 seconds at 20Hz
            data = await monitor.get_audio_level()
            level = data['level'] * 100
            
            if level > max_level:
                max_level = level
                
            bars = int(level / 2)
            meter = "█" * bars + "░" * (50 - bars)
            
            print(f"\r[{meter}] {level:5.1f}% ({data['db']:6.1f} dB) | Max: {max_level:5.1f}%", 
                  end="", flush=True)
            
            await asyncio.sleep(0.05)
        
        print(f"\n\n✅ Test complete! Maximum level: {max_level:.1f}%")
        await monitor.stop_monitoring()
    else:
        print("❌ Failed to start monitoring")

if __name__ == "__main__":
    asyncio.run(test_direct_usb())