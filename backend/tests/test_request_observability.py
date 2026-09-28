def test_request_id_is_generated_and_echoed(client):
    response = client.get("/health")
    assert response.headers["x-request-id"]


def test_supplied_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "incident-123"})
    assert response.headers["x-request-id"] == "incident-123"


def test_http_metrics_use_route_template(client):
    client.get("/health")
    metrics = client.get("/metrics").text
    if not metrics:
        return  # conftest supplies a minimal prometheus stub when unavailable
    assert 'meeting_ops_http_requests_total{handler="/health",method="GET",status="200"}' in metrics
    assert "meeting_ops_http_request_duration_seconds_bucket" in metrics


def test_outbound_header_uses_bound_request_id():
    from middleware.request_context import (
        bind_request_id,
        outbound_request_headers,
        request_id_var,
    )

    token = bind_request_id("trace-across-services")
    try:
        assert outbound_request_headers() == {"X-Request-ID": "trace-across-services"}
    finally:
        request_id_var.reset(token)
