#!/usr/bin/env python3
"""
Wrapper to ensure audio works properly
"""
import os
import sys

# Set the library path before any imports
os.environ['LD_PRELOAD'] = '/lib/x86_64-linux-gnu/libstdc++.so.6'
os.environ['PYTHONUNBUFFERED'] = '1'

# Test sounddevice first
try:
    import sounddevice as sd
    devices = sd.query_devices()
    print(f"✅ Sounddevice working! Found {len(devices) if hasattr(devices, '__len__') else 1} devices")
except Exception as e:
    print(f"❌ Sounddevice test failed: {e}")
    sys.exit(1)

# Now import and run the main app
print("🚀 Starting server with working audio...")
from main import app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9050)