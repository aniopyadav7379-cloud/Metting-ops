#!/usr/bin/env python3
import requests
import time
import os

API = "http://localhost:9050"

print("🧪 FINAL AUDIO TEST")
print("=" * 50)

# 1. Check audio status before recording
print("\n1️⃣ Audio status BEFORE recording:")
status = requests.get(f"{API}/api/debug/audio-status").json()
print(f"   use_sounddevice: {status['use_sounddevice']}")
print(f"   is_recording: {status['is_recording']}")
print(f"   callbacks: {status['callbacks']}")
print(f"   has_stream: {status['has_stream']}")

# 2. Create and start recording
resp = requests.post(f"{API}/api/recording-sessions", 
                    json={"name": "Final Test", "description": "Final audio test"})
session_id = resp.json()["session"]["session_id"]
print(f"\n2️⃣ Created session: {session_id}")

resp = requests.post(f"{API}/api/recording-sessions/{session_id}/start")
print(f"   Start result: {resp.json()['success']}")

# 3. Check audio status during recording
print("\n3️⃣ Audio status DURING recording:")
status = requests.get(f"{API}/api/debug/audio-status").json()
print(f"   use_sounddevice: {status['use_sounddevice']}")
print(f"   is_recording: {status['is_recording']}")
print(f"   has_stream: {status['has_stream']}")
print(f"   sample_rate: {status['sample_rate']}")
print(f"   audio_queue_size: {status['audio_queue_size']}")

# 4. Wait and check queue
print("\n4️⃣ Monitoring for 10 seconds...")
for i in range(10):
    time.sleep(1)
    status = requests.get(f"{API}/api/debug/audio-status").json()
    print(f"   {i+1}s: queue_size={status['audio_queue_size']}, is_recording={status['is_recording']}")

# 5. Stop and check results
resp = requests.post(f"{API}/api/recording-sessions/{session_id}/stop")
final = resp.json()["session"]
print(f"\n5️⃣ Recording stopped:")
print(f"   Duration: {final['duration_seconds']}s")
print(f"   Status: {final['status']}")

# 6. Check file
audio_file = f"storage/audio_files/recordings/{session_id}/recording_*.wav"
files = [f for f in os.listdir(f"storage/audio_files/recordings/{session_id}/") if f.endswith('.wav')]
if files:
    file_path = f"storage/audio_files/recordings/{session_id}/{files[0]}"
    file_size = os.path.getsize(file_path)
    print(f"\n6️⃣ Audio file: {files[0]}")
    print(f"   Size: {file_size} bytes")
    print(f"   Expected size for 10s: ~{16000 * 2 * 10} bytes")
else:
    print("\n6️⃣ ❌ No audio file found!")

print("\n" + "=" * 50)