# speaker-svc — pyannote community-1 diarization + wespeaker embeddings
# Targets: bigboy GPU 1 (Quadro RTX 6000), CUDA 12.x, joined to meet-internal network
#
# Build:  docker build -t meet-speaker-svc:local .
# Run:    docker run -d --gpus '"device=1"' -p 8889:8889 \
#           -e HUGGINGFACE_TOKEN=... -v /opt/speaker-models:/models \
#           --name meet-speaker-svc meet-speaker-svc:local
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    SPEECHBRAIN_FETCH_LOCAL=true \
    TORCH_HOME=/models/torch

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        ffmpeg libsndfile1 libsox-fmt-all sox \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3 1

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --upgrade pip wheel \
    && python -m pip install --extra-index-url https://download.pytorch.org/whl/cu126 -r requirements.txt

# Pyannote models are gated; they are downloaded on first startup/request when
# HUGGINGFACE_TOKEN is set and the user has accepted the model terms.

COPY . .

EXPOSE 8889

# Healthcheck runs /healthz/synthetic — the full-pipeline probe that loads the
# bundled 2-speaker fixture, diarizes it, and asserts speaker_count == 2. This
# catches pipeline-state degradation that /health (HTTP-up only) misses. See
# task #105 for context. start-period is 2m so model cold-load on first probe
# doesn't trip the healthcheck; subsequent probes run on warm models in <60s.
# Compose-level overrides on individual deploys may extend interval/start-period
# if the host needs more headroom.
HEALTHCHECK --interval=5m --timeout=60s --retries=2 --start-period=120s \
    CMD curl -fsS http://localhost:8889/healthz/synthetic || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8889", "--workers", "1"]
