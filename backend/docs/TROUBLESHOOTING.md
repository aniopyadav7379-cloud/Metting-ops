# Troubleshooting Guide

*Last Updated: January 22, 2025*

## Overview

This document provides solutions to common issues encountered while developing and deploying Unicorn Commander.

## Resolved Issues

### 1. FastAPI Startup Hanging

**Problem**: FastAPI server hangs during startup, particularly when initializing the SpeakerDiarizer.

**Symptoms**:
- Server starts but doesn't respond to requests
- Startup messages incomplete
- No error messages displayed

**Root Cause**: The SpeakerDiarizer initialization was being called synchronously during startup, blocking the event loop.

**Solution**: 
```python
# Incorrect - blocks startup
@app.on_event("startup")
async def startup_event():
    await transcription_service.initialize()  # This was hanging

# Correct - initialize synchronously
def initialize_services():
    # Initialize services synchronously
    transcription_service = TranscriptionService()
    # Other initializations...

# Call before app creation
initialize_services()
app = FastAPI()
```

**Status**: ✅ RESOLVED

### 2. Embedded GitHub Token Error in Build

**Problem**: Build process for llvm-project embeds GitHub tokens in build artifacts, causing security warnings and build failures.

**Symptoms**:
- Build fails with GitHub token detection errors
- Security warnings about exposed credentials
- NPU tools cannot be built successfully

**Root Cause**: The clone-llvm.sh script inherits GitHub environment variables (GITHUB_TOKEN, GH_TOKEN) which get embedded in the build.

**Planned Solution**:
```bash
# Modify clone-llvm.sh to unset GitHub tokens before cloning
unset GITHUB_TOKEN
unset GH_TOKEN
git clone https://github.com/llvm/llvm-project.git
```

**Status**: 🔧 SOLUTION IDENTIFIED - Implementation pending

### 3. Audio File Saving (0-byte files)

**Problem**: Audio recording created files but they were always 0 bytes.

**Root Cause**: Audio data wasn't being properly written to the file during recording.

**Solution**: Fixed the audio buffer writing logic in the file storage service.

**Status**: ✅ RESOLVED

### 4. Ollama Connection Error

**Problem**: Summarization endpoint failed with "Failed to fetch" error.

**Root Cause**: Ollama service wasn't running or wasn't accessible at localhost:11434.

**Solution**: 
1. Ensure Ollama is installed and running
2. Start Ollama service: `ollama serve`
3. Verify connection: `curl http://localhost:11434/api/generate`

**Status**: ✅ RESOLVED

## Common Issues

### 1. NPU Not Detected

**Symptoms**: 
- System falls back to CPU execution
- No performance improvements observed

**Possible Causes**:
1. NPU drivers not installed
2. ONNX Runtime missing DirectML provider
3. NPU hardware not present

**Solutions**:
1. Install AMD NPU drivers
2. Install ONNX Runtime with DirectML support
3. Verify NPU hardware: `lspci | grep -i npu`

### 2. Audio Device Not Found

**Symptoms**:
- "No audio devices found" error
- Recording fails to start

**Solutions**:
1. Check audio permissions: `sudo usermod -a -G audio $USER`
2. List available devices: `python -c "import sounddevice; print(sounddevice.query_devices())"`
3. Verify ALSA configuration: `aplay -l`

### 3. WebSocket Connection Failed

**Symptoms**:
- Real-time transcription not working
- "WebSocket connection failed" in browser console

**Solutions**:
1. Check CORS configuration in backend
2. Verify WebSocket URL matches backend address
3. Ensure no proxy is blocking WebSocket connections

### 4. Database Migration Errors

**Symptoms**:
- "Table already exists" errors
- Missing columns in database

**Solutions**:
1. Reset database: `rm meeting_sessions.db`
2. Re-run migrations: `python -m alembic upgrade head`
3. Check migration history: `python -m alembic history`

## Performance Issues

### 1. Slow Transcription

**Possible Causes**:
- Running on CPU instead of NPU
- Using large Whisper model
- Insufficient memory

**Solutions**:
1. Verify NPU is being used (check logs)
2. Use whisper-base or whisper-small model
3. Monitor memory usage during transcription

### 2. High Memory Usage

**Symptoms**:
- System becomes unresponsive
- Out of memory errors

**Solutions**:
1. Limit audio buffer size
2. Use smaller Whisper models
3. Enable swap if needed
4. Monitor with: `htop` or `free -h`

## Debugging Tools

### 1. Enable Debug Logging

```python
# In main.py
import logging
logging.basicConfig(level=logging.DEBUG)
```

### 2. Monitor NPU Usage

```bash
# AMD NPU monitoring (when available)
amd-npu-smi

# Check kernel logs
dmesg | grep -i npu
```

### 3. Profile Performance

```python
# Enable ONNX Runtime profiling
options = ort.SessionOptions()
options.enable_profiling = True
options.profile_file_prefix = "profile"
```

### 4. Test Audio Input

```python
# Test audio recording
python -c "
import sounddevice as sd
import numpy as np
duration = 5  # seconds
fs = 16000
recording = sd.rec(int(duration * fs), samplerate=fs, channels=1)
sd.wait()
print(f'Recorded {len(recording)} samples')
print(f'Max amplitude: {np.max(np.abs(recording))}')
"
```

## Getting Help

If you encounter issues not covered here:

1. Check the logs in `backend/logs/`
2. Review the GitHub issues
3. Enable debug logging for more details
4. Collect system information:
   - OS version: `uname -a`
   - Python version: `python --version`
   - NPU status: `lspci | grep -i npu`
   - Audio devices: `aplay -l`

## Contributing

If you solve a new issue, please update this document with:
1. Clear description of the problem
2. Steps to reproduce
3. Root cause analysis
4. Solution that worked
5. Current status