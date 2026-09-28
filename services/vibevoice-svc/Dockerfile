# vibevoice-svc — Microsoft VibeVoice long-form, multi-speaker TTS for Meeting-Ops
# Targets: midboy1 GPU 1 (Tesla P40, 24 GB), CUDA 12.x, joined to host network or LAN.
#
# Build:  docker build -t meet-vibevoice:local .
# Run:    docker run -d --gpus '"device=1"' -p 8882:8882 \
#           -v /opt/vibevoice-models:/models meet-vibevoice:local
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    VIBEVOICE_CACHE=/models/vibevoice \
    VIBEVOICE_VOICES_DIR=/app/voices \
    VIBEVOICE_EAGER_LOAD=true

# Swap Ubuntu mirrors to ones reachable from midboy1's network path.
# canonical's archive.ubuntu.com IPv4 stalls on this host's uplink during
# build, so we point apt at mirrors.edge.kernel.org for both main + security
# (it carries jammy + jammy-security/updates).
RUN sed -i 's|http://archive.ubuntu.com/ubuntu/|http://mirrors.edge.kernel.org/ubuntu/|g; s|http://security.ubuntu.com/ubuntu/|http://mirrors.edge.kernel.org/ubuntu/|g' \
        /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=120 update \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        ffmpeg lame libsndfile1 \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

WORKDIR /app

COPY requirements.txt .

# CUDA wheels for the P40 (Pascal, sm_61). 2.4.x has cu121 wheels that work fine
# on the host driver; the cudnn runtime base image supplies the rest.
RUN python -m pip install --upgrade pip wheel \
    && python -m pip install --extra-index-url https://download.pytorch.org/whl/cu121 \
        torch==2.4.1 torchaudio==2.4.1 \
    && python -m pip install -r requirements.txt

# Pull the VibeVoice TTS code and install it in-place.
# Microsoft pulled the original TTS code from microsoft/VibeVoice in 2025-09;
# the community fork at vibevoice-community/VibeVoice preserves it. We bring
# the source tree in via the build context (extracted from the tarball below)
# rather than `git clone` because the docker buildkit network on this host
# stalls on github.com clones. Refresh by re-running the snippet in
# README.md or `services/vibevoice-svc/refresh-source.sh`.
COPY VibeVoice-src/ /opt/vibevoice/
RUN python -m pip install /opt/vibevoice \
    # Copy the bundled voice presets out so the service's VOICES_DIR has them.
    && mkdir -p /app/voices \
    && cp -a /opt/vibevoice/demo/voices/*.wav /app/voices/ 2>/dev/null \
    && ls -la /app/voices

COPY main.py /app/main.py

EXPOSE 8882

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=180s \
    CMD curl -fsS http://localhost:8882/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8882", "--workers", "1"]
