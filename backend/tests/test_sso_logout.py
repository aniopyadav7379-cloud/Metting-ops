"""Tests for GET /api/auth/sso-logout — the full SSO logout endpoint.

Regression coverage for the prod logout bounce: on the native-OIDC deploy
(no oauth2-proxy) the handler must actually END the Keycloak SSO session using
the id_token stashed at login, otherwise the SPA's zero-click SSO silently
re-authenticates the user straight back in. See auth/routes.py::sso_logout and
auth/oidc_sso.py (IDT_COOKIE_NAME / uc_sso_callback).

NOTE: auth.oidc_sso is imported INSIDE the tests, not at module level. The `app`
fixture reloads auth.models against the isolated test DB; a collection-time
import of any auth.* module would bind to the pre-reload models and split the
SQLAlchemy mapper registry, breaking other tests (see tests/conftest.py).
"""
import base64
import json
from urllib.parse import unquote


def _kc():
    """Logout config the handler derives its redirect from (read post-fixture)."""
    from auth.oidc_sso import (
        APP_PUBLIC_URL,
        IDT_COOKIE_NAME,
        IDT_COOKIE_PATH,
        KC_CLIENT_ID,
        KC_ISSUER,
        SESSION_COOKIE_NAME,
    )

    return {
        "app_url": APP_PUBLIC_URL,
        "idt_name": IDT_COOKIE_NAME,
        "idt_path": IDT_COOKIE_PATH,
        "client_id": KC_CLIENT_ID,
        "session_name": SESSION_COOKIE_NAME,
        "end_session": f"{KC_ISSUER}/protocol/openid-connect/logout",
    }


def _set_cookies(response):
    """All Set-Cookie header values on a response (httpx multidict)."""
    return response.headers.get_list("set-cookie")


def test_sso_logout_prod_native_oidc_ends_kc_session(client):
    """Prod (cookie session, no Authorization header): redirect to KC end-session
    with id_token_hint + client_id, and delete both app cookies."""
    cfg = _kc()
    resp = client.get(
        "/api/auth/sso-logout",
        headers={"Cookie": f"{cfg['session_name']}=app.jwt.value; {cfg['idt_name']}=kc.id.token"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = unquote(resp.headers["location"])
    assert loc.startswith(cfg["end_session"]), loc
    assert f"client_id={cfg['client_id']}" in loc
    assert "id_token_hint=kc.id.token" in loc
    assert f"post_logout_redirect_uri={cfg['app_url']}/" in loc

    cookies = _set_cookies(resp)
    # Both cookies cleared, each at the path it was originally set with.
    assert any(cfg["session_name"] in c and "Max-Age=0" in c and "Path=/;" in c for c in cookies), cookies
    assert any(
        cfg["idt_name"] in c and "Max-Age=0" in c and f"Path={cfg['idt_path']}" in c
        for c in cookies
    ), cookies


def test_sso_logout_prod_without_stored_idt(client):
    """Session predating the idt cookie: still hit KC end-session with client_id,
    just without id_token_hint (KC then shows its one-click confirm page)."""
    cfg = _kc()
    resp = client.get(
        "/api/auth/sso-logout",
        headers={"Cookie": f"{cfg['session_name']}=app.jwt.value"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = unquote(resp.headers["location"])
    assert loc.startswith(cfg["end_session"]), loc
    assert f"client_id={cfg['client_id']}" in loc
    assert "id_token_hint" not in loc


def test_sso_logout_oauth2_proxy_path_unchanged(client):
    """Dogfood (oauth2-proxy injects Bearer id_token): unchanged — bounce through
    the proxy's /oauth2/sign_out so its session cookie is cleared too."""
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"iss": "https://auth.magicunicorn.dev/realms/uchub", "azp": "meeting-ops"}
        ).encode()
    ).rstrip(b"=").decode()
    bearer = f"header.{payload}.sig"

    resp = client.get(
        "/api/auth/sso-logout",
        headers={"Authorization": f"Bearer {bearer}"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("/oauth2/sign_out?rd="), loc
    assert "auth.magicunicorn.dev" in unquote(loc)


def test_sso_logout_unauthenticated_goes_home(client):
    """No Authorization header and no session cookie (local dev / already logged
    out): just send the SPA home, which routes to login when unauthenticated."""
    resp = client.get("/api/auth/sso-logout", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
