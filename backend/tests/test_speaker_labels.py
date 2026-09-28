"""Tests for services/speaker_labels.py — humane speaker labels everywhere.

Pins the v3.33.0 guarantees:
  - raw pyannote codes (SPEAKER_00 …) never survive into display labels,
  - real names are untouched,
  - "Speaker N" output is stable/idempotent (re-normalizing changes nothing),
  - the attributed transcript merges consecutive same-speaker segments and
    prefixes every line with the display label (what the summarizer reads).
"""

from services.speaker_labels import (
    build_attributed_transcript,
    is_raw_label,
    normalized_label_map,
)


def test_is_raw_label_variants():
    assert is_raw_label("SPEAKER_00")
    assert is_raw_label("SPEAKER 7")
    assert is_raw_label("speaker_3")
    assert is_raw_label("SPEAKER-12")
    assert not is_raw_label("Aaron Stransky")
    assert not is_raw_label("Speaker 1")  # already-normalized form
    assert not is_raw_label("")
    assert not is_raw_label(None)


def test_map_orders_by_first_appearance():
    m = normalized_label_map(["SPEAKER_02", "Aaron Stransky", "SPEAKER_00"])
    assert m["SPEAKER_02"] == "Speaker 1"
    assert m["SPEAKER_00"] == "Speaker 2"
    assert m["Aaron Stransky"] == "Aaron Stransky"


def test_map_idempotent_and_reserves_existing_numbers():
    # A session partially normalized before: "Speaker 1" exists; the raw
    # leftover must NOT collide with it.
    m = normalized_label_map(["Speaker 1", "SPEAKER_05"])
    assert m["Speaker 1"] == "Speaker 1"
    assert m["SPEAKER_05"] == "Speaker 2"
    # Idempotent: running on its own output is the identity.
    again = normalized_label_map(list(m.values()))
    assert all(k == v for k, v in again.items())


def test_attributed_transcript_merges_and_prefixes():
    segments = [
        {"speaker": "SPEAKER_00", "text": "hello there", "start": 0, "end": 1},
        {"speaker": "SPEAKER_00", "text": "how are you", "start": 1, "end": 2},
        {"speaker": "Aaron Stransky", "text": "good thanks", "start": 2, "end": 3},
        {"speaker": "SPEAKER_00", "text": "great", "start": 3, "end": 4},
        {"speaker": "SPEAKER_01", "text": "", "start": 4, "end": 5},  # empty -> dropped
    ]
    text, speakers = build_attributed_transcript(segments)
    lines = text.split("\n")
    assert lines[0] == "Speaker 1: hello there how are you"  # merged
    assert lines[1] == "Aaron Stransky: good thanks"
    assert lines[2] == "Speaker 1: great"
    assert speakers == ["Speaker 1", "Aaron Stransky"]
    assert "SPEAKER_00" not in text and "SPEAKER_01" not in text


def test_attributed_transcript_empty_segments():
    text, speakers = build_attributed_transcript([])
    assert text == "" and speakers == []
