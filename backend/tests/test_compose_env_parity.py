"""Guard against worker/backend env drift in the bigboy compose.

The Arq reprocess worker (`meet-bulk-import-worker`) runs the SAME STT ->
diarize -> summarize -> index pipeline as the `backend`, so it needs the same
model-service endpoints. When it silently lacked them, every server-side
reprocess failed ("Temporary failure in name resolution / no segments") and
recordings never produced a summary. This test fails if the worker is missing
any model-service env var the backend declares, so the drift can't recur.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "bigboy"
    / "docker-compose.bigboy.yml"
)
ENV_EXAMPLE = COMPOSE.with_name(".env.bigboy.example")
PROJECTOPS_LIFECYCLE_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "projectops_lifecycle.py"
)

# Names whose absence on the worker breaks the reprocess pipeline.
SERVICE_VAR_RE = re.compile(
    r"^(PARAKEET|SORTFORMER|SPEAKER|EMBEDDING|INFINITY|RERANKER|LLM_MODEL|"
    r"MEETING_OPS_LLM|MEETING_OPS_SUMMARIZER|OPENAI|LLAMA|QDRANT|GARAGE)"
)


def _env_names(service: dict) -> set[str]:
    env = service.get("environment", [])
    names: set[str] = set()
    if isinstance(env, list):
        for item in env:
            names.add(str(item).split("=", 1)[0].strip())
    elif isinstance(env, dict):
        names.update(str(k) for k in env)
    return names


def _env_pairs(service: dict) -> dict[str, str]:
    """Return the service's environment as a name -> value dict.

    Mirrors _env_names but keeps the value so we can inspect URL targets.
    Handles both compose env forms (``- KEY=VALUE`` list and ``KEY: VALUE``
    mapping)."""
    env = service.get("environment", [])
    pairs: dict[str, str] = {}
    if isinstance(env, list):
        for item in env:
            name, _, value = str(item).partition("=")
            pairs[name.strip()] = value
    elif isinstance(env, dict):
        for k, v in env.items():
            pairs[str(k)] = "" if v is None else str(v)
    return pairs


# Model-service URL vars whose host, when it names a co-located meet-* svc,
# must correspond to a real service in the same compose file. URLs that point
# at an external host (e.g. a Tailscale IP) are intentionally ignored.
SERVICE_URL_VARS = ("SPEAKER_SVC_URL", "PARAKEET_SERVER_URL", "SORTFORMER_URL")
# Capture the host out of http://<host>:<port>, restricted to meet-* hosts so
# the ${VAR:-http://...} default form still resolves the right target.
MEET_HOST_RE = re.compile(r"https?://(meet-[A-Za-z0-9_.-]+):\d+")


def test_worker_has_backend_model_service_env():
    if not COMPOSE.exists():
        pytest.skip(f"compose not found at {COMPOSE}")
    data = yaml.safe_load(COMPOSE.read_text())
    services = data.get("services", {})
    backend = services.get("backend")
    worker = services.get("meet-bulk-import-worker")
    assert backend and worker, "backend / meet-bulk-import-worker services missing"

    backend_service_vars = {v for v in _env_names(backend) if SERVICE_VAR_RE.match(v)}
    worker_vars = _env_names(worker)
    missing = sorted(backend_service_vars - worker_vars)
    assert not missing, (
        "meet-bulk-import-worker is missing model-service env the backend "
        "declares; the reprocess pipeline will fail (Parakeet/LLM unreachable). "
        "Add these to the worker service env: " + ", ".join(missing)
    )


def test_sentry_env_is_present_on_backend_and_worker():
    data = yaml.safe_load(COMPOSE.read_text())
    services = data["services"]
    for service_name in ("backend", "meet-bulk-import-worker"):
        env_names = _env_names(services[service_name])
        assert {"SENTRY_DSN", "SENTRY_ENVIRONMENT"} <= env_names


def test_arq_queue_lanes_are_wired_to_backend_and_workers():
    data = yaml.safe_load(COMPOSE.read_text())
    services = data["services"]
    required = {"ARQ_BATCH_QUEUE", "ARQ_INTERACTIVE_QUEUE", "ARQ_INTERACTIVE_WORKERS"}
    for service_name in ("backend", "meet-bulk-import-worker", "meet-interactive-worker"):
        assert required <= _env_names(services[service_name])
    assert services["meet-bulk-import-worker"]["container_name"] != services["meet-interactive-worker"]["container_name"]


def test_retention_env_is_present_on_backend_and_worker():
    data = yaml.safe_load(COMPOSE.read_text())
    services = data["services"]
    required = {"MEETING_RETENTION_ENABLED", "MEETING_RETENTION_DAYS", "MEETING_RETENTION_MAX_PER_RUN"}
    for service_name in ("backend", "meet-bulk-import-worker"):
        assert required <= _env_names(services[service_name])


def test_projectops_public_url_is_wired_to_the_canonical_bigboy_origin():
    """The stored human backlink must not fall back to an unrelated domain."""
    data = yaml.safe_load(COMPOSE.read_text())
    expected = "${PROJECTOPS_PUBLIC_URL:-https://projectops.magicunicorn.dev}"
    for service_name in ("backend", "meet-bulk-import-worker"):
        assert _env_pairs(data["services"][service_name]).get(
            "PROJECTOPS_PUBLIC_URL"
        ) == expected

    assert (
        "PROJECTOPS_PUBLIC_URL=https://projectops.magicunicorn.dev"
        in ENV_EXAMPLE.read_text()
    )
    source = PROJECTOPS_LIFECYCLE_SOURCE.read_text()
    assert (
        '"PROJECTOPS_PUBLIC_URL", "https://projectops.magicunicorn.dev"'
        in source
    )


def test_meet_host_model_urls_have_a_matching_container():
    """If a service points SPEAKER_SVC_URL / PARAKEET_SERVER_URL / SORTFORMER_URL
    at ``http://meet-<svc>:<port>``, a service with that ``container_name`` must
    exist in the same compose file.

    This catches the drift where someone wires a model-service URL at a
    co-located meet-* container that was never defined (or got renamed/removed)
    — the reprocess + live pipelines would then fail name resolution at runtime.
    External targets (e.g. a Tailscale-IP host) are intentionally ignored; only
    in-compose meet-* hosts are asserted."""
    if not COMPOSE.exists():
        pytest.skip(f"compose not found at {COMPOSE}")
    data = yaml.safe_load(COMPOSE.read_text())
    services = data.get("services", {})

    container_names = {
        svc.get("container_name")
        for svc in services.values()
        if isinstance(svc, dict) and svc.get("container_name")
    }

    missing: list[str] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        pairs = _env_pairs(svc)
        for var in SERVICE_URL_VARS:
            if var not in pairs:
                continue
            m = MEET_HOST_RE.search(pairs[var])
            if not m:
                continue
            host = m.group(1)
            if host not in container_names:
                missing.append(f"{svc_name}.{var} -> {host}")

    assert not missing, (
        "compose service(s) reference a meet-* model-service host with no "
        "matching container_name in the same file (name resolution will fail "
        "at runtime): " + ", ".join(sorted(missing))
    )
