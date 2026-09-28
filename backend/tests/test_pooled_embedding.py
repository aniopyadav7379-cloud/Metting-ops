"""Tests for speaker_service.pooled_embedding (v3.34.0 pooled voiceprints).

NOTE: services.speaker_service is imported lazily (inside a fixture), NOT at
module level — a module-level import during collection initializes
database.models before auth.models, splitting the SQLAlchemy Base registry
and breaking DB-touching suites collected in the same run with
NoReferencedTableError (FK 'users').
"""

import numpy as np
import pytest


@pytest.fixture()
def pooled_embedding():
    # Lazy import: keep the SQLAlchemy Base registry intact when this file is
    # collected alongside DB-touching suites (see module docstring).
    import auth.models  # noqa: F401  (users FK must register first)
    from services.speaker_service import pooled_embedding as fn

    return fn


def _unit(v):
    a = np.asarray(v, dtype="float32")
    return a / np.linalg.norm(a)


def test_prefers_short_band_over_long_mush(pooled_embedding):
    # One 300s "mush" turn pointing one way, three 10s clean turns the other.
    mush = {"start": 0, "end": 300, "embedding": [1.0, 0.0, 0.0]}
    clean = [
        {"start": 300 + i * 10, "end": 310 + i * 10, "embedding": [0.0, 1.0, 0.0]}
        for i in range(3)
    ]
    out = np.asarray(pooled_embedding([mush] + clean))
    # Pool must come from the in-band 10s turns only — orthogonal to the mush.
    assert float(np.dot(out, _unit([1.0, 0.0, 0.0]))) < 1e-6
    assert float(np.dot(out, _unit([0.0, 1.0, 0.0]))) > 0.999


def test_falls_back_to_longest_when_no_band(pooled_embedding):
    # Only out-of-band turns: a 0.5s (unusable) and a 300s one -> use the 300s.
    items = [
        {"start": 0, "end": 0.5, "embedding": [0.0, 1.0]},
        {"start": 1, "end": 301, "embedding": [3.0, 0.0]},
    ]
    out = np.asarray(pooled_embedding(items))
    assert np.allclose(out, [1.0, 0.0], atol=1e-6)  # normalized longest


def test_caps_at_top_k_longest_in_band(pooled_embedding):
    # 7 in-band turns; the 5 longest all point +x, the 2 shortest point +y.
    items = [
        {"start": 0, "end": 40 - i, "embedding": [1.0, 0.0]} for i in range(5)
    ] + [
        {"start": 0, "end": 3, "embedding": [0.0, 1.0]},
        {"start": 0, "end": 2.5, "embedding": [0.0, 1.0]},
    ]
    out = np.asarray(pooled_embedding(items))
    assert np.allclose(out, [1.0, 0.0], atol=1e-6)


def test_returns_none_without_embeddings(pooled_embedding):
    assert pooled_embedding([]) is None
    assert pooled_embedding([{"start": 0, "end": 10}]) is None
    assert pooled_embedding(None) is None


def test_output_is_unit_norm(pooled_embedding):
    out = pooled_embedding([{"start": 0, "end": 10, "embedding": [2.0, 2.0, 1.0]}])
    assert abs(np.linalg.norm(np.asarray(out)) - 1.0) < 1e-5
