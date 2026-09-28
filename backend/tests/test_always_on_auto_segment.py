"""Tests for the v3.18.0 ALWAYS_ON_AUTO_SEGMENT default-off behavior.

The always-on recorder used to enter CHECKING_CONTEXT (and possibly split the
session) after >5 minutes of silence. As of v3.18.0 that behavior is gated by
the ALWAYS_ON_AUTO_SEGMENT env var (default ``"0"``). When off, silence resets
the counter and keeps the same session — one meeting stays one session unless
the user explicitly stops. The watchdog (v3.17.1) still catches abandoned
recordings on its own 30-min cron.
"""
from __future__ import annotations

import importlib

import pytest


def _fresh_recorder():
    import services.always_on_recorder as mod
    importlib.reload(mod)
    return mod.AlwaysOnRecorder()


def test_auto_segment_default_off(monkeypatch):
    monkeypatch.delenv("ALWAYS_ON_AUTO_SEGMENT", raising=False)
    rec = _fresh_recorder()
    assert rec._auto_segment_enabled is False
    assert rec.get_status()["auto_segment_enabled"] is False


def test_auto_segment_on_when_env_set(monkeypatch):
    monkeypatch.setenv("ALWAYS_ON_AUTO_SEGMENT", "1")
    rec = _fresh_recorder()
    assert rec._auto_segment_enabled is True
    assert rec.get_status()["auto_segment_enabled"] is True


@pytest.mark.parametrize("val", ["true", "yes", "on", "TRUE", "Yes"])
def test_auto_segment_on_accepts_aliases(monkeypatch, val):
    monkeypatch.setenv("ALWAYS_ON_AUTO_SEGMENT", val)
    rec = _fresh_recorder()
    assert rec._auto_segment_enabled is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_auto_segment_off_for_falsy(monkeypatch, val):
    monkeypatch.setenv("ALWAYS_ON_AUTO_SEGMENT", val)
    rec = _fresh_recorder()
    assert rec._auto_segment_enabled is False
