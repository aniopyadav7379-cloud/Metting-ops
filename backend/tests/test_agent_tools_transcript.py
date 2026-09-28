"""RAG agent transcript extraction parity — no raw diarizer codes reach the model.

Regression for the fix where ``services/agent_tools._extract_transcript_text``
emitted raw ``seg["speaker"]`` (e.g. ``SPEAKER_00``) instead of routing through
the app-wide ``build_attributed_transcript`` normalizer that the per-meeting chat
(``api/ai_chat.py``) and the summarizer already use. The cross-meeting RAG agent
must surface "Speaker N" / real names exactly like the per-meeting chat does for
the same session — otherwise un-normalized sessions (satellite/companion uploads,
legacy rows) leak ``SPEAKER_00`` into RAG answers and citations.
"""
from types import SimpleNamespace

from services.agent_tools import _extract_transcript_text


def _session(**kw):
    base = dict(transcript_diarized=None, transcript_simple=None, transcript=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_diarized_raw_codes_are_normalized():
    s = _session(transcript_diarized={"segments": [
        {"speaker": "SPEAKER_00", "text": "Hello."},
        {"speaker": "SPEAKER_01", "text": "Hi there."},
        {"speaker": "SPEAKER_00", "text": "Lets start."},
    ]})
    out = _extract_transcript_text(s)
    assert "SPEAKER_00" not in out and "SPEAKER_01" not in out
    assert "Speaker 1:" in out and "Speaker 2:" in out


def test_real_names_pass_through():
    s = _session(transcript_diarized={"segments": [
        {"speaker": "Aaron Stransky", "text": "Welcome."},
    ]})
    assert "Aaron Stransky:" in _extract_transcript_text(s)


def test_attributed_preferred_over_flat_simple():
    # Mirrors ai_chat.py: the diarized/attributed transcript wins over the flat
    # transcript_simple (which carries no per-line speaker attribution).
    s = _session(
        transcript_diarized={"segments": [
            {"speaker": "SPEAKER_00", "text": "Attributed line."},
        ]},
        transcript_simple="flat unattributed text",
    )
    out = _extract_transcript_text(s)
    assert "Speaker 1: Attributed line." in out
    assert out != "flat unattributed text"


def test_falls_back_to_simple_when_no_segments():
    s = _session(transcript_diarized={"segments": []}, transcript_simple="flat text")
    assert _extract_transcript_text(s) == "flat text"
