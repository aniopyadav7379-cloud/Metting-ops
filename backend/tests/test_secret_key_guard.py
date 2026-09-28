"""security-2: SECRET_KEY fail-closed boot guard.

A weak/known/empty JWT signing key is a full auth bypass (anyone can forge
access tokens). auth.config._resolve_secret_key refuses to boot on a weak key in
any real deployment (ENVIRONMENT unset or non-dev), and only allows an insecure
dev fallback when ENVIRONMENT marks a dev/test/CI run.
"""
from __future__ import annotations

import pytest

import auth.config as cfg


STRONG = "x" * 40  # >= 32 chars, not a placeholder


def test_strong_key_passes_regardless_of_env(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("SECRET_KEY", STRONG)
    assert cfg._resolve_secret_key() == STRONG


def test_unset_env_unset_key_refuses_boot(monkeypatch):
    # No ENVIRONMENT (=> treated as a real deployment) + no key => fail-closed.
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        cfg._resolve_secret_key()


def test_prod_placeholder_refuses_boot(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "your-secret-key-change-this-in-production")
    with pytest.raises(RuntimeError):
        cfg._resolve_secret_key()


def test_prod_short_key_refuses_boot(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "tooshort")
    with pytest.raises(RuntimeError):
        cfg._resolve_secret_key()


def test_relaxed_env_weak_key_uses_dev_fallback(monkeypatch):
    # dev/test/CI may run without a configured key — insecure fallback, no crash.
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    assert cfg._resolve_secret_key() == cfg._DEV_FALLBACK_SECRET
