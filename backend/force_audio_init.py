#!/usr/bin/env python3
"""Force audio initialization for testing"""
import os
os.environ['LD_PRELOAD'] = '/lib/x86_64-linux-gnu/libstdc++.so.6'

import sys
sys.path.append('/srv/meeting-ops/backend')

from services.audio_input_service import audio_input_service
import time

print("🔧 Forcing audio initialization...")

# Initialize audio
if audio_input_service.initialize("default", 16000, 1):
    print("✅ Audio initialized successfully!")
else:
    print("❌ Audio initialization failed!")

# Register test callbacks
def test_audio_data(session_id, audio_bytes):
    print(f"📝 Audio data callback: {len(audio_bytes)} bytes")

def test_audio_chunk(session_id, chunk_bytes):
    print(f"📦 Audio chunk callback: {len(chunk_bytes)} bytes")

audio_input_service.register_callback('audio_data', test_audio_data)
audio_input_service.register_callback('audio_chunk', test_audio_chunk)
print("✅ Callbacks registered")

# Start recording
if audio_input_service.start_recording("test_session"):
    print("✅ Recording started!")
    
    # Record for 5 seconds
    print("🎙️ Recording for 5 seconds...")
    time.sleep(5)
    
    # Stop recording
    audio_input_service.stop_recording()
    print("✅ Recording stopped!")
else:
    print("❌ Failed to start recording")