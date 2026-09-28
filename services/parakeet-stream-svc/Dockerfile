# meet-parakeet-stream-svc — Phase B.2 real Parakeet 0.6B v3 streaming service.
# Targets: midboy2 GPU 0 (RTX 3060, 12 GB), CUDA 12.x.
#
# Base layer: the already-built meet-parakeet-svc:local image. This is the
# CUDA + NeMo 2.4.1 + torch 2.4.1 + audio-tooling stack that the batch
# transcribe container runs on. Reusing it means:
#   - no second 5-8 GB CUDA download / install,
#   - identical NeMo behaviour (model class + signature),
#   - shared model cache via the parakeet_models named volume,
#   - build time on midboy2 is seconds rather than 30+ minutes.
#
# Build:  docker build -t meet-parakeet-stream-svc:local .
#         (assumes meet-parakeet-svc:local already exists locally on the
#         host — which is the case on midboy2 today).
# Run via docker-compose.midboy2.yml.
FROM meet-parakeet-svc:local

ENV PARAKEET_STREAM_MODEL=nvidia/parakeet-tdt-0.6b-v3 \
    PARAKEET_STREAM_PRECISION=fp16 \
    PARAKEET_STREAM_PORT=8895 \
    SERVICE_NAME=meet-parakeet-stream-svc

WORKDIR /app

# Replace the parakeet-svc main.py with the streaming version. We share
# the entire model-loading + NeMo stack from the base image but expose a
# different endpoint shape (/healthz + /transcribe-stream returning
# {text, segments, confidence}) tuned for the near-streaming chunked
# windows the meet-backend WS handler sends.
COPY main.py /app/main.py
COPY requirements.txt /app/requirements.txt

# requirements.txt only adds whatever the stream service needs ON TOP of
# the base image. Today that's nothing — the base already has fastapi,
# uvicorn, pydantic, soundfile, torchaudio, NeMo. The file exists so we
# can add things without rebuilding the world.
RUN python -m pip install --no-cache-dir -r requirements.txt

EXPOSE 8895

# Healthcheck mirrors parakeet-svc's: /healthz is cheap (no GPU touch);
# real readiness is signalled by the model_loaded flag inside the JSON.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=120s \
    CMD curl -fsS http://localhost:8895/healthz || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8895", "--workers", "1"]
