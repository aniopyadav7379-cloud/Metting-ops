def test_security_headers_are_applied(client):
    response = client.get("/health")

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy-report-only"]
    assert "strict-transport-security" not in response.headers


def test_hsts_is_applied_for_forwarded_https(client):
    response = client.get("/health", headers={"X-Forwarded-Proto": "https"})

    assert response.headers["strict-transport-security"] == (
        "max-age=63072000; includeSubDomains"
    )
