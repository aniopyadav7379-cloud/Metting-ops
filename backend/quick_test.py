#!/usr/bin/env python3
import requests
import time

# Quick test
print("Quick audio test...")

# Check status
status = requests.get("http://localhost:9050/api/debug/audio-status").json()
print(f"Instance: {status['instance_id']}, use_sounddevice: {status['use_sounddevice']}")

# Create and start recording
resp = requests.post("http://localhost:9050/api/recording-sessions", 
                    json={"name": "Quick", "description": "Test"})
session_id = resp.json()["session"]["session_id"]

resp = requests.post(f"http://localhost:9050/api/recording-sessions/{session_id}/start")
print(f"Recording started: {resp.json()['success']}")

# Check status again
time.sleep(2)
status = requests.get("http://localhost:9050/api/debug/audio-status").json()
print(f"During recording - Instance: {status['instance_id']}, is_recording: {status['is_recording']}, has_stream: {status['has_stream']}")

# Stop
resp = requests.post(f"http://localhost:9050/api/recording-sessions/{session_id}/stop")
print(f"Duration: {resp.json()['session']['duration_seconds']}s")