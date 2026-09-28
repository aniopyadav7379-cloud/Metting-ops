# Backend Dockerfile - Python 3.12 + NPU Support
FROM python:3.12-slim

# Install system dependencies for NPU and audio. alsa-utils ships
# arecord, which the Conference Room setup wizard uses to enumerate
# USB capture devices via `arecord -l` and which the RoomRecorder
# service spawns to capture audio from a specific hw:X,Y ALSA device.
RUN apt-get update && apt-get install -y \
    build-essential \
    portaudio19-dev \
    ffmpeg \
    git \
    curl \
    cmake \
    libasound2-dev \
    alsa-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Make the entrypoint executable. The script runs `alembic upgrade head`
# before exec'ing uvicorn so alembic is the canonical schema source and
# new tables never get raced in by create_all without their FK/index
# additions.
RUN chmod +x /app/docker-entrypoint.sh

# Create directories for data and NPU kernels
RUN mkdir -p /app/data /app/recordings /app/whisper_onnx_cache /app/npu_kernels

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DATABASE_URL=sqlite:////app/data/meeting_sessions.db
ENV PYTHON_VERSION=3.13
ENV NPU_KERNEL_CACHE_DIR=/app/npu_kernels

# NPU/XRT environment (when available)
ENV XRT_INI_PATH=/opt/xilinx/xrt/xrt.ini
ENV XILINX_XRT=/opt/xilinx/xrt

# Expose port
EXPOSE 9050

# Run the application via the alembic-first entrypoint.
ENTRYPOINT ["/app/docker-entrypoint.sh"]