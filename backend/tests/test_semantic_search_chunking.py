"""Speaker-aware chunking robustness (audit finding: turn-split regex).

Index transcripts are built internally as "Name: text" lines, but the old
turn-split regex ([A-Z][a-zA-Z\\s]*?) silently missed legitimate display
names — "Mary-Jane:", "O'Brien:", "Dr. Smith:", "José:", "SPEAKER_00:" —
merging those turns into the previous speaker's chunk and dropping them
from the per-chunk speakers payload. The split is now a permissive
per-line ^(.{1,60}?):\\s match. These tests pin the new behavior, the
plain-text fallback, and the rewritten overlap block (whose misaligned
speakers/turns zip was deleted).
"""
from __future__ import annotations

from services.semantic_search_service import (
    CHUNK_SIZE,
    _match_speaker_label,
    semantic_search,
)


def test_match_speaker_label_handles_real_display_names():
    cases = {
        "Mary-Jane: I can take that one.": "Mary-Jane",
        "O'Brien: agreed.": "O'Brien",
        "Dr. Smith: let's review the labs.": "Dr. Smith",
        "José: hola a todos.": "José",
        "Speaker 1: opening remarks.": "Speaker 1",
        "SPEAKER_00: unidentified voice.": "SPEAKER_00",
        "[Speaker 2]: bracketed legacy form.": "Speaker 2",
    }
    for line, expected in cases.items():
        assert _match_speaker_label(line) == expected, line


def test_match_speaker_label_rejects_non_label_lines():
    assert _match_speaker_label("just a plain continuation line") is None
    assert _match_speaker_label("") is None
    # A colon past the 60-char cap is prose, not a label.
    assert _match_speaker_label(("x" * 61) + ": tail") is None
    # No whitespace after the colon -> not a turn label (e.g. URLs).
    assert _match_speaker_label("https://example.com/path") is None


def test_chunk_text_splits_turns_for_all_name_shapes():
    transcript = "\n".join([
        "Mary-Jane: I think we should ship on Friday.",
        "O'Brien: Agreed, pending the QA pass.",
        "Dr. Smith: I'll review the compliance checklist.",
        "José: I can own the rollout comms.",
        "Speaker 1: Any objections?",
        "SPEAKER_00: None from me.",
    ])

    chunks = semantic_search._chunk_text(transcript)

    assert len(chunks) == 1  # short transcript -> single chunk
    assert chunks[0]["speakers"] == sorted([
        "Mary-Jane", "O'Brien", "Dr. Smith", "José", "Speaker 1", "SPEAKER_00",
    ])
    assert chunks[0]["text"] == transcript


def test_chunk_text_plain_fallback_keeps_empty_speakers():
    plain = "no speaker labels anywhere in this text just words " * 3
    chunks = semantic_search._chunk_text(plain.strip())
    assert len(chunks) == 1
    assert chunks[0]["speakers"] == []


def test_continuation_lines_stay_with_their_speaker():
    transcript = "\n".join([
        "O'Brien: first part of the thought",
        "and this continuation line has no label",
        "Mary-Jane: a different speaker now",
    ])
    chunks = semantic_search._chunk_speaker_text(transcript)
    assert len(chunks) == 1
    assert chunks[0]["speakers"] == ["Mary-Jane", "O'Brien"]
    # Continuation stays glued to O'Brien's turn, before Mary-Jane's label.
    text = chunks[0]["text"]
    assert text.index("continuation") < text.index("Mary-Jane:")


def test_long_transcript_chunks_with_overlap_and_correct_speakers():
    # Force multiple chunks: alternating speakers, each turn ~40 words, with
    # enough total words to exceed CHUNK_SIZE several times over.
    speakers = ["Mary-Jane", "O'Brien", "Dr. Smith", "José"]
    turn_body = " ".join(f"word{i}" for i in range(40))
    lines = [
        f"{speakers[i % len(speakers)]}: {turn_body}"
        for i in range(3 * (CHUNK_SIZE // 40))
    ]
    transcript = "\n".join(lines)

    chunks = semantic_search._chunk_text(transcript)

    assert len(chunks) > 1
    for chunk in chunks:
        # Every speaker credited on a chunk actually labels a line in it,
        # and every labelled line is credited — no misattribution.
        labelled = {
            _match_speaker_label(line)
            for line in chunk["text"].split("\n")
            if _match_speaker_label(line)
        }
        assert set(chunk["speakers"]) == labelled
        assert labelled, "speaker chunks should always carry speakers"

    # The overlap block carries trailing turns into the next chunk.
    first_tail = chunks[0]["text"].split("\n")[-1]
    assert first_tail in chunks[1]["text"]
