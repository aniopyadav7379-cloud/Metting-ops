"""Pytest fixtures for speaker-svc unit tests.

We don't want the tests to actually load pyannote / wespeaker models — they
are gigabytes and need a HuggingFace token. The /healthz/synthetic tests
mock out _run_diarization_on_wav at the boundary instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `import main` work when pytest is run from anywhere.
SVC_ROOT = Path(__file__).resolve().parent.parent
if str(SVC_ROOT) not in sys.path:
    sys.path.insert(0, str(SVC_ROOT))
