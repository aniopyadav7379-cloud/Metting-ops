"""Tests for Keycloak id_token verification in auth/oidc_sso.

The UC SSO callback previously trusted ``jwt.get_unverified_claims`` — any
well-formed JWT (even self-signed, expired, or minted for another client) would
have been accepted and auto-provisioned, including ``is_superuser`` off a forged
``groups`` claim. ``_verify_id_token`` now enforces RS256 signature (via the
realm JWKS) + issuer + audience (azp OR aud == client_id) + expiry.

These tests mock the JWKS client with a locally-generated RSA keypair so the
real PyJWT verify path runs without network. ``auth.oidc_sso`` is imported
lazily inside the test bodies (not at module top) so collection time doesn't
bind app modules to a pre-reload Base.metadata — the conftest reloads the model
graph in a session fixture, and a collection-time bind causes a mapper split
that breaks later DB-touching tests.
"""
from __future__ import annotations

import time
import types

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(autouse=True)
def _reloaded_app(app):
    """Run the conftest model-reload (the session ``app`` fixture, which reloads
    the model graph and imports ``main`` → all routers) BEFORE these tests touch
    ``auth.oidc_sso``. Without it a pure-unit test that runs first would bind the
    app modules to the pre-reload Base, causing a mapper split in later DB tests."""
    return app


def _oidc():
    from auth import oidc_sso
    return oidc_sso


def _make_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub


@pytest.fixture(scope="module")
def keypair():
    return _make_keypair()


@pytest.fixture()
def patch_jwks(keypair, monkeypatch):
    """Point _get_jwks_client at the test public key (no network)."""
    _priv, pub = keypair
    fake = types.SimpleNamespace(
        get_signing_key_from_jwt=lambda _t: types.SimpleNamespace(key=pub)
    )
    monkeypatch.setattr(_oidc(), "_get_jwks_client", lambda: fake)
    return keypair


def _token(priv, **over):
    oidc = _oidc()
    now = int(time.time())
    payload = {
        "iss": oidc.KC_ISSUER,
        "aud": oidc.KC_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "email": "user@example.com",
        "preferred_username": "user",
    }
    payload.update(over)
    return pyjwt.encode(payload, priv, algorithm="RS256")


def test_valid_token_accepted(patch_jwks):
    priv, _ = patch_jwks
    claims = _oidc()._verify_id_token(_token(priv))
    assert claims["email"] == "user@example.com"


def test_valid_via_azp_when_aud_is_other(patch_jwks):
    # aud is some resource server, but azp is our client → accepted.
    priv, _ = patch_jwks
    claims = _oidc()._verify_id_token(_token(priv, aud="account", azp=_oidc().KC_CLIENT_ID))
    assert claims["azp"] == _oidc().KC_CLIENT_ID


def test_wrong_issuer_rejected(patch_jwks):
    priv, _ = patch_jwks
    with pytest.raises(pyjwt.InvalidIssuerError):
        _oidc()._verify_id_token(_token(priv, iss="https://evil.example/realms/x"))


def test_wrong_audience_rejected(patch_jwks):
    priv, _ = patch_jwks
    with pytest.raises(pyjwt.InvalidAudienceError):
        _oidc()._verify_id_token(_token(priv, aud="other-client", azp="other-client"))


def test_expired_rejected(patch_jwks):
    priv, _ = patch_jwks
    past = int(time.time()) - 3600
    with pytest.raises(pyjwt.ExpiredSignatureError):
        _oidc()._verify_id_token(_token(priv, exp=past, iat=past))


def test_missing_exp_rejected(patch_jwks):
    priv, _ = patch_jwks
    oidc = _oidc()
    now = int(time.time())
    tok = pyjwt.encode(
        {"iss": oidc.KC_ISSUER, "aud": oidc.KC_CLIENT_ID, "iat": now, "email": "u@x.com"},
        priv, algorithm="RS256",
    )
    with pytest.raises(pyjwt.MissingRequiredClaimError):
        oidc._verify_id_token(tok)


def test_forged_signature_rejected(patch_jwks):
    # Token signed by a DIFFERENT key than the JWKS serves → signature fails.
    other_priv, _ = _make_keypair()
    forged = _token(other_priv, groups=["uc-admins"])  # would have escalated
    with pytest.raises(pyjwt.InvalidSignatureError):
        _oidc()._verify_id_token(forged)
