#!/usr/bin/env python3
"""
Direct test of audio recording with transcription
"""
import os
os.environ['LD_PRELOAD'] = '/lib/x86_64-linux-gnu/libstdc++.so.6'

import sys
sys.path.append('.')

import time
import wave
import numpy as np
from services.audio_input_service import audio_input_service
from services.audio_recording_service import audio_recording_service
from services.transcription_service import transcription_service

print("🧪 Direct Audio Recording Test with Transcription")
print("=" * 50)

# Initialize audio service
print("\n1️⃣ Initializing audio service...")
if audio_input_service.initialize("default", 16000, 1):
    print(f"✅ Audio initialized (instance {audio_input_service.instance_id})")
    print(f"   use_sounddevice: {audio_input_service.use_sounddevice}")
else:
    print("❌ Failed to initialize audio")
    sys.exit(1)

# Set up audio file recording
session_id = "test_session_direct"
audio_file = f"test_recording_{int(time.time())}.wav"
print(f"\n2️⃣ Setting up file recording: {audio_file}")
audio_recording_service.start_recording(session_id, audio_file, 16000, 1)

# Register callbacks
audio_chunks = []
transcriptions = []

def handle_audio_data(sess_id, audio_bytes):
    # Save to file
    audio_recording_service.write_audio_chunk(sess_id, audio_bytes)
    # Keep for analysis
    audio_chunks.append(audio_bytes)

def handle_audio_chunk(sess_id, chunk_bytes):
    print(f"📦 Received {len(chunk_bytes)} byte chunk for transcription")
    # Process transcription
    try:
        result = transcription_service.process_audio_chunk(chunk_bytes, session_id=sess_id)
        if result:
            print(f"📝 Transcription: {result.text}")
            transcriptions.append(result.text)
    except Exception as e:
        print(f"❌ Transcription error: {e}")

audio_input_service.register_callback('audio_data', handle_audio_data)
audio_input_service.register_callback('audio_chunk', handle_audio_chunk)
print("✅ Callbacks registered")

# Start recording
print(f"\n3️⃣ Starting recording...")
if audio_input_service.start_recording(session_id):
    print("✅ Recording started!")
    print("🎙️ Recording for 15 seconds (should get 1-2 chunks)...")
    
    # Monitor for 15 seconds
    for i in range(15):
        print(f"   {i+1}/15s - chunks: {len(audio_chunks)}, queue: {audio_input_service.audio_queue.qsize()}")
        time.sleep(1)
    
    # Stop recording
    print("\n4️⃣ Stopping recording...")
    audio_input_service.stop_recording()
    audio_recording_service.stop_recording(session_id)
    
    # Results
    print(f"\n5️⃣ Results:")
    print(f"   Audio chunks received: {len(audio_chunks)}")
    print(f"   Total audio bytes: {sum(len(c) for c in audio_chunks)}")
    print(f"   Transcriptions: {len(transcriptions)}")
    if transcriptions:
        print(f"   Text: {' '.join(transcriptions)}")
    
    # Check file
    if os.path.exists(audio_file):
        file_size = os.path.getsize(audio_file)
        print(f"   Audio file size: {file_size} bytes")
        if file_size > 0:
            print("   ✅ Audio successfully recorded!")
    else:
        print("   ❌ Audio file not found")
else:
    print("❌ Failed to start recording")

print("\n" + "=" * 50)
print("✅ Test complete!")