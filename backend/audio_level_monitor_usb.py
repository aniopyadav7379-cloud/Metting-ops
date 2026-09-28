#!/usr/bin/env python3
"""
USB Microphone Audio Level Monitor
Specifically designed to work with USB microphones
"""

import asyncio
import numpy as np
import logging
from typing import Optional, Dict, Any
import json
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import pyaudio
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    logger.warning("PyAudio not available, using simulation mode")
    PYAUDIO_AVAILABLE = False
    pyaudio = None

class AudioLevelMonitor:
    """Monitor audio levels from USB microphone"""
    
    def __init__(self, device_index: Optional[int] = None, simulate: bool = False):
        self.device_index = device_index
        self.sample_rate = 44100  # Standard USB mic rate
        self.chunk_size = 1024
        self.channels = 1
        self.format = pyaudio.paInt16 if PYAUDIO_AVAILABLE else None
        self.audio = None
        self.stream = None
        self.monitoring = False
        # Force real audio - only simulate if explicitly told or no PyAudio
        self.simulate = simulate or not PYAUDIO_AVAILABLE
        self.simulation_time = 0
        
        if self.simulate:
            logger.warning("⚠️  Starting in SIMULATION mode!")
        else:
            logger.info("🎤 Starting in REAL AUDIO mode")
        
    def find_usb_microphone(self) -> Optional[int]:
        """Find USB microphone device index"""
        if not PYAUDIO_AVAILABLE or self.audio is None:
            return None
            
        try:
            device_count = self.audio.get_device_count()
            logger.info(f"Found {device_count} audio devices")
            
            # Look for USB PnP Sound Device which is our USB mic
            for i in range(device_count):
                try:
                    info = self.audio.get_device_info_by_index(i)
                    name = info.get('name', '').lower()
                    
                    # Check for our specific USB mic
                    if 'usb pnp sound device' in name and info['maxInputChannels'] > 0:
                        logger.info(f"Found USB PnP Sound Device at index {i}: {info['name']}")
                        return i
                except:
                    pass
            
            # Otherwise, search all devices
            for i in range(device_count):
                try:
                    info = self.audio.get_device_info_by_index(i)
                    name = info.get('name', '').lower()
                    
                    # Log all devices for debugging
                    logger.info(f"Device {i}: {info['name']} - Inputs: {info['maxInputChannels']}")
                    
                    # Check if it's an input device
                    if info['maxInputChannels'] > 0:
                        # Look for common USB microphone names
                        if any(usb_indicator in name for usb_indicator in ['usb', 'blue', 'yeti', 'webcam', 'c920', 'logitech']):
                            logger.info(f"Found USB microphone: {info['name']} at index {i}")
                            return i
                except Exception as e:
                    logger.warning(f"Error checking device {i}: {e}")
                    continue
            
            # Try the 'default' device first
            for i in range(device_count):
                try:
                    info = self.audio.get_device_info_by_index(i)
                    if 'default' in info.get('name', '').lower() and info['maxInputChannels'] > 0:
                        logger.info(f"Using default audio device: {info['name']} at index {i}")
                        logger.info("   This should capture from your system's default microphone")
                        return i
                except:
                    continue
            
            # If no default found, use first available input device
            for i in range(device_count):
                try:
                    info = self.audio.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        logger.info(f"Using first available input: {info['name']} at index {i}")
                        return i
                except:
                    continue
                    
        except Exception as e:
            logger.error(f"Error finding USB microphone: {e}")
            
        return None
        
    def start_monitoring(self) -> bool:
        """Start audio monitoring"""
        if self.simulate:
            logger.info("📊 Starting simulated audio monitor (PyAudio not available)")
            self.monitoring = True
            self.simulation_time = 0
            return True
            
        try:
            self.audio = pyaudio.PyAudio()
            
            # Find USB microphone
            if self.device_index is None:
                self.device_index = self.find_usb_microphone()
                
            if self.device_index is None:
                logger.error("❌ No microphone found - devices scanned but none suitable")
                logger.error("   This will cause SIMULATION mode")
                # Fall back to simulation
                self.simulate = True
                self.monitoring = True
                logger.warning("⚠️  FALLING BACK TO SIMULATION MODE")
                return True
                
            device_info = self.audio.get_device_info_by_index(self.device_index)
            logger.info(f"📊 Starting audio monitor on: {device_info['name']}")
            logger.info(f"   Sample rate: {self.sample_rate} Hz")
            logger.info(f"   Channels: {self.channels}")
            
            # Try to open stream with our preferred settings
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
                # Try with device's default settings
                self.sample_rate = int(device_info['defaultSampleRate'])
                self.channels = min(device_info['maxInputChannels'], 2)  # Use stereo if available
                logger.info(f"Retrying with device defaults - Rate: {self.sample_rate}, Channels: {self.channels}")
                
                self.stream = self.audio.open(
                    format=self.format,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=self.chunk_size
                )
            
            self.monitoring = True
            logger.info("✅ Audio monitoring started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start audio monitoring: {e}")
            # Fall back to simulation
            self.simulate = True
            self.monitoring = True
            return True
            
    def stop_monitoring(self):
        """Stop audio monitoring"""
        self.monitoring = False
        
        if self.stream and not self.simulate:
            self.stream.stop_stream()
            self.stream.close()
            
        if self.audio and not self.simulate:
            self.audio.terminate()
            
        logger.info("Audio monitoring stopped")
        
    def get_audio_level(self) -> Dict[str, Any]:
        """Get current audio level"""
        if self.simulate:
            # Generate simulated audio levels
            import math
            self.simulation_time += 0.1
            
            # Create interesting pattern
            base = 0.3
            wave1 = 0.2 * math.sin(self.simulation_time * 2)
            wave2 = 0.1 * math.sin(self.simulation_time * 5)
            noise = np.random.random() * 0.1
            
            level = base + wave1 + wave2 + noise
            level = max(0, min(1, level))  # Clamp to 0-1
            
            # Add occasional peaks
            if np.random.random() < 0.02:
                level = min(1, level + 0.3)
                
            db = 20 * np.log10(level + 1e-10)
            
            return {
                "type": "audio_level",
                "level": level,
                "peak": level,
                "db": db,
                "timestamp": datetime.now().isoformat(),
                "simulated": True
            }
            
        if not self.monitoring or not self.stream:
            return {
                "type": "audio_level",
                "level": 0,
                "peak": 0,
                "db": -60,
                "timestamp": datetime.now().isoformat(),
                "error": "Not monitoring"
            }
            
        try:
            # Read audio data
            data = self.stream.read(self.chunk_size, exception_on_overflow=False)
            
            # Convert to numpy array
            audio_data = np.frombuffer(data, dtype=np.int16)
            
            # If stereo, convert to mono
            if self.channels == 2:
                audio_data = audio_data.reshape(-1, 2).mean(axis=1)
            
            # Normalize to 0-1 range
            max_value = 32768.0  # Max value for 16-bit audio
            audio_data = audio_data / max_value
            
            # Calculate RMS level
            rms = np.sqrt(np.mean(audio_data ** 2))
            
            # Calculate peak
            peak = np.max(np.abs(audio_data))
            
            # Convert to dB
            db = 20 * np.log10(rms + 1e-10)  # Add small value to avoid log(0)
            
            return {
                "type": "audio_level",
                "level": float(rms),
                "peak": float(peak),
                "db": float(db),
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error reading audio level: {e}")
            return {
                "type": "audio_level",
                "level": 0,
                "peak": 0,
                "db": -60,
                "timestamp": datetime.now().isoformat(),
                "error": str(e)
            }