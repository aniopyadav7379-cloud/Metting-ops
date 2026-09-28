# Multi-Environment Architecture for Unicorn Commander

*Last Updated: January 22, 2025*

## Overview

To support both Python 3.12 (for ML dependencies) and Python 3.13 (for NPU optimization), we can use a microservices approach where different components run in their optimal Python environments.

## Current Implementation Status

### Working Components:
1. **WebSocket Implementation** - Real-time audio streaming and live transcription broadcasting
2. **ONNX Whisper Integration** - Using whisper-base model for transcription (CPU mode)
3. **Ollama Integration** - Connected at localhost:11434 for meeting summarization
4. **Audio File Saving** - Fixed and working correctly
5. **Authentication System** - JWT-based auth with admin user creation

### NPU Acceleration Status:
- ONNX Whisper is currently running on CPU
- NPU acceleration kernels are implemented but need hardware testing
- MLIR-AIE2 kernels ready for AMD NPU Phoenix
- Performance optimizations in place, awaiting hardware validation

## Architecture Options

### Option 1: Poetry with Environment Markers

Using Poetry, we can specify different dependencies for different Python versions:

```toml
[tool.poetry.dependencies]
python = ">=3.11,<3.14"

# Core dependencies work with all versions
fastapi = "^0.115.14"
numpy = "^2.3.1"
onnxruntime = "^1.22.0"

# ML dependencies only for Python <3.13
pyannote-audio = {version = "^3.1.1", python = "<3.13", optional = true}
whisperx = {version = "^3.1.1", python = "<3.13", optional = true}

[tool.poetry.extras]
ml = ["pyannote-audio", "whisperx"]
```

Install with: `poetry install -E ml` (for Python 3.12)
Install without ML: `poetry install` (for Python 3.13)

### Option 2: Separate Virtual Environments

```bash
# Main API (Python 3.13 for NPU)
python3.13 -m venv venv-api
source venv-api/bin/activate
pip install -r requirements-api.txt

# ML Services (Python 3.12)
python3.12 -m venv venv-ml
source venv-ml/bin/activate
pip install -r requirements-ml.txt
```

### Option 3: Docker Microservices

```yaml
version: '3.8'
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile.api
    image: python:3.13-slim
    
  ml-service:
    build:
      context: .
      dockerfile: Dockerfile.ml
    image: python:3.12-slim
```

### Option 4: Process Separation with IPC

Run different services as separate processes communicating via:
- Unix sockets
- Redis pub/sub
- RabbitMQ
- gRPC

## Recommended Approach: Hybrid Architecture

### 1. Main API Service (Python 3.13)
- FastAPI web server
- Database operations
- WebSocket handling
- Basic ONNX inference
- NPU-optimized operations

### 2. ML Service (Python 3.12)
- WhisperX transcription
- Pyannote.audio diarization
- Heavy ML operations
- Communicates via REST API or message queue

### Implementation Steps:

#### Step 1: Create Service Launcher
```python
# launcher.py
import subprocess
import os
import sys

def start_services():
    # Start ML service with Python 3.12
    ml_process = subprocess.Popen([
        '/path/to/python3.12', '-m', 'services.ml_service'
    ])
    
    # Start API with Python 3.13
    api_process = subprocess.Popen([
        '/path/to/python3.13', '-m', 'uvicorn', 'main:app'
    ])
    
    return ml_process, api_process
```

#### Step 2: ML Service Interface
```python
# services/ml_service.py
from fastapi import FastAPI
import whisperx
import pyannote.audio

ml_app = FastAPI()

@ml_app.post("/transcribe")
async def transcribe(audio_path: str):
    # Use WhisperX with Python 3.12
    result = whisperx.transcribe(audio_path)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(ml_app, host="127.0.0.1", port=9051)
```

#### Step 3: Main API Integration
```python
# main.py (Python 3.13)
import httpx

async def transcribe_audio(audio_path: str):
    # Call ML service running on Python 3.12
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:9051/transcribe",
            json={"audio_path": audio_path}
        )
        return response.json()
```

## Benefits of This Approach:

1. **Optimal Python Version**: Each component runs in its ideal Python environment
2. **NPU Support**: Python 3.13 can fully utilize NPU optimizations
3. **ML Compatibility**: Python 3.12 maintains compatibility with ML libraries
4. **Scalability**: Services can be scaled independently
5. **Fault Isolation**: ML service crashes don't affect the main API
6. **Easy Updates**: Update components independently

## Environment Setup Scripts:

### setup-py313.sh
```bash
#!/bin/bash
python3.13 -m venv venv-py313
source venv-py313/bin/activate
pip install -r requirements-core.txt
pip install -r requirements-npu.txt
```

### setup-py312.sh
```bash
#!/bin/bash
python3.12 -m venv venv-py312
source venv-py312/bin/activate
pip install -r requirements-ml.txt
```

### run-all.sh
```bash
#!/bin/bash
# Start ML service in background
source venv-py312/bin/activate
python -m services.ml_service &
ML_PID=$!

# Start main API
source venv-py313/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 9050 &
API_PID=$!

# Wait for both
wait $ML_PID $API_PID
```

## Configuration:

### config.yaml
```yaml
services:
  ml:
    host: "127.0.0.1"
    port: 9051
    python_version: "3.12"
    
  api:
    host: "0.0.0.0"
    port: 9050
    python_version: "3.13"
    
npu:
  enabled: true
  provider: "DmlExecutionProvider"
```

This architecture provides the best of both worlds: NPU optimization with Python 3.13 and ML library compatibility with Python 3.12.

## Live Transcription WebSocket Architecture

### WebSocket Endpoint
```
WS /ws/stream/{session_id}
```

### Message Flow:
1. **Client connects** to WebSocket with session ID
2. **Audio chunks** (10 seconds) are processed by ONNX Whisper
3. **Transcriptions** are broadcast to all connected clients
4. **Database storage** happens in parallel

### WebSocket Message Format:
```json
{
  "type": "transcription",
  "session_id": "string",
  "chunk_id": "string",
  "text": "transcribed text",
  "timestamp": "ISO 8601",
  "speaker_id": "optional"
}
```

## Ollama Integration

### Configuration:
- **Host**: localhost
- **Port**: 11434
- **Model**: gemma3n (3B parameters)
- **Endpoint**: POST /api/intelligence/summarize

### Summarization Flow:
1. Gather all transcriptions for a session
2. Send to Ollama API at http://localhost:11434/api/generate
3. Parse streaming response
4. Return formatted summary

### Example Request:
```python
async def summarize_session(session_id: str):
    transcriptions = await get_session_transcriptions(session_id)
    prompt = f"Summarize this meeting: {transcriptions}"
    
    response = await ollama_client.generate(
        model="gemma3n",
        prompt=prompt,
        stream=False
    )
    
    return response['response']
```