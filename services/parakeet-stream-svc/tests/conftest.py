"""Pytest fixtures for parakeet-stream-svc unit tests.

The streaming service is heavy (NeMo + torch + CUDA). The opus_decoder
module is intentionally self-contained — it only needs opuslib +
numpy — so its tests don't need any of that machinery.

We add the service root to sys.path so `import opus_decoder` works
regardless of where pytest is invoked from.
"""
from __future__ import annotations

import sys
from pathlib import Path

SVC_ROOT = Path(__file__).resolve().parent.parent
if str(SVC_ROOT) not in sys.path:
    sys.path.insert(0, str(SVC_ROOT))
