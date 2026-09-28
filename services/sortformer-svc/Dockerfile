# meet-sortformer-svc — Phase B.3 spike: NVIDIA Sortformer streaming
# diarization. Targets midboy2 GPU 0 (RTX 3060, 12 GB), CUDA 12.x.
#
# Base layer: the already-built meet-parakeet-svc:local image. Same
# reasoning as parakeet-stream-svc: that image ships CUDA 12.4 +
# NeMo 2.4.1 + torch 2.4.1 + soundfile + torchaudio + huggingface_hub,
# all of which Sortformer needs. Rebuilding the world from a CUDA
# base image would take 30+ minutes and ~5-8 GB of duplicated layers.
#
# Layering means:
#   - build time on midboy2 is seconds (just COPY + a tiny pip install),
#   - we inherit the parakeet_models named volume so the NeMo HF cache
#     is shared with parakeet-svc + parakeet-stream-svc (one download
#     of nvidia/diar_sortformer_4spk-v1, all three containers see it),
#   - identical NeMo behaviour to the streaming + batch ASR services,
#     so a NeMo/torch bug surfaces in all three and we fix once.
#
# Build:  docker build -t meet-sortformer-svc:b3-spike .
#         (assumes meet-parakeet-svc:local already exists locally on
#         the host — which is the case on midboy2 today)
# Run via docker-compose.midboy2.yml additions documented in the
# spike report (docs/phase-b3-sortformer-spike.md).
FROM meet-parakeet-svc:local

ENV SORTFORMER_MODEL=nvidia/diar_sortformer_4spk-v1 \
    SORTFORMER_PRECISION=fp16 \
    SORTFORMER_PORT=8896 \
    SORTFORMER_BATCH_SIZE=1 \
    SERVICE_NAME=meet-sortformer-svc

WORKDIR /app

# Replace the parakeet-svc main.py with the sortformer service. We
# share the entire model-loading + NeMo + audio tooling stack from
# the base image but expose a diarization-only endpoint shape:
#   GET  /health     — liveness + model-loaded flag
#   POST /diarize-stream — per-window WAV/PCM body in, speakers JSON out
COPY main.py /app/main.py
COPY requirements.txt /app/requirements.txt
COPY tests /app/tests

# requirements.txt only adds whatever the sortformer service needs ON TOP
# of the base image. Today that's nothing extra — NeMo 2.4.1 in the base
# already includes SortformerEncLabelModel + every dep needed to load
# nvidia/diar_sortformer_4spk-v1. The file exists so we can add things
# (e.g. opuslib if we ever accept Opus uplink directly) without
# rebuilding the world.
RUN python -m pip install --no-cache-dir -r requirements.txt

EXPOSE 8896

# Healthcheck mirrors parakeet-stream-svc's: /health is cheap (no GPU
# touch), readiness is signalled by the model_loaded flag inside the
# JSON. start-period is 180 s to let NeMo finish loading the 123 M
# Sortformer params from the HF cache (typically 2-3 s warm,
# 30-60 s on first download).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=180s \
    CMD curl -fsS http://localhost:8896/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8896", "--workers", "1"]
