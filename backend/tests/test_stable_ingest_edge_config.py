"""Lock the narrowly scoped Stable-to-Meeting-Ops Traefik trust boundary."""

# ruff: noqa: S101

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
EDGE_CONFIG = (
    REPO_ROOT
    / "deploy"
    / "bigboy"
    / "traefik"
    / "dynamic"
    / "meetingops-stable-ingest.yml"
)
COMPOSE = REPO_ROOT / "deploy" / "bigboy" / "docker-compose.bigboy.yml"

EXPECTED_RULE = "Host(`meetingops.magicunicorn.dev`) && Path(`/api/v1/sessions/ingest`)"


@pytest.fixture(scope="module")
def edge_config() -> dict:
    return yaml.safe_load(EDGE_CONFIG.read_text())


def test_edge_file_contains_only_the_exact_stable_ingest_route(
    edge_config: dict,
) -> None:
    assert set(edge_config) == {"http"}
    http = edge_config["http"]
    assert set(http) == {"routers", "services"}
    assert set(http["routers"]) == {"meetingops-stable-ingest"}
    assert set(http["services"]) == {"meetingops-stable-ingest"}

    router = http["routers"]["meetingops-stable-ingest"]
    assert router["rule"] == EXPECTED_RULE
    assert "PathPrefix" not in router["rule"]
    assert router["entryPoints"] == ["websecure"]
    assert router["priority"] == 300
    assert router["service"] == "meetingops-stable-ingest"
    assert router["tls"] == {"certResolver": "letsencrypt"}


def test_edge_route_preserves_bearer_and_bypasses_oauth2_proxy(
    edge_config: dict,
) -> None:
    router = edge_config["http"]["routers"]["meetingops-stable-ingest"]
    assert "middlewares" not in router

    service = edge_config["http"]["services"]["meetingops-stable-ingest"]
    assert service == {
        "loadBalancer": {
            "passHostHeader": True,
            "servers": [{"url": "http://meet-backend:9050"}],
        }
    }


def test_direct_backend_target_is_reachable_on_traefik_web_network() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    backend = compose["services"]["backend"]
    assert backend["container_name"] == "meet-backend"
    assert "web" in backend["networks"]
    assert compose["networks"]["web"]["external"] is True
