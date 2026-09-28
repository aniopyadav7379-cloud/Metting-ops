# parakeet-openai-shim — OpenAI-compatible STT shim in front of parakeet-svc.
# Stateless, CPU-only, tiny. No GPU, no models — it just translates the OpenAI
# /v1/audio/transcriptions contract to parakeet-svc /transcribe and meters usage.
# Runs anywhere it can reach PARAKEET_URL (typically alongside parakeet-svc on
# midboy1 / bigboy, on the same internal network).
#
# Build:  docker build -t meet-parakeet-openai-shim:local .
# Run:    docker run -d -p 8011:8011 \
#           -e PARAKEET_URL=http://meet-parakeet-svc:8881 \
#           -e METERED_REPORT_URL=http://ops-center:8084/api/v1/metered/report \
#           -e FEDERATION_KEY=... \
#           --name meet-parakeet-openai-shim meet-parakeet-openai-shim:local
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SHIM_PORT=8011

# curl for the healthcheck only.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip wheel \
    && python -m pip install -r requirements.txt

COPY . .

EXPOSE 8011

# Plain liveness probe — the shim holds no model state, so HTTP-up is sufficient.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -fsS http://localhost:${SHIM_PORT}/health || exit 1

# Single worker is plenty: the shim is I/O-bound (it forwards to parakeet-svc),
# and parakeet-svc itself serializes on one GPU. Scale with replicas if needed.
# Use the shell form so ${SHIM_PORT} is expanded at runtime (env-overridable).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${SHIM_PORT} --workers 1"]
