"""Ordering guard for the primary record->stop completion path (v3.29.2).

`simple_recording_db.process_recording` is the main record->stop pipeline. It
used to call `semantic_search.index_session` BEFORE `identify_speakers`, so the
vector copy of every normal meeting embedded raw SPEAKER_xx labels instead of
real names — degrading cross-meeting RAG attribution until a reprocess healed
it — even though the inline comment claimed "rewrite labels BEFORE embedding".
v3.29.2 swapped them (identify first, then index built FROM the now
speaker-named diarized segments, mirroring reprocess Stage-5.9).

The equivalent SERVICE-LEVEL behavior (index_session called with a
speaker-named transcript after identify) is already pinned end-to-end for the
reprocess pipeline in test_reprocess_indexing.py. Driving the full
process_recording here would need ~6 stubbed leaf calls (STT, diarize, assign,
vocab, summary LLM, identify, index), most of them function-local imports — a
brittle harness for what is fundamentally an ordering invariant. So this guard
pins the invariant directly + cheaply: in process_recording's source, the
identify_speakers() call must precede the index_session() call. If a future
refactor reintroduces the bug by moving the blocks, this goes red.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _process_recording_source() -> str:
    """Read just the function source without importing hardware-heavy modules."""
    path = Path(__file__).resolve().parents[1] / "api" / "simple_recording_db.py"
    source = path.read_text()
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "process_recording"
    )
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def test_process_recording_starts_diarization_before_awaiting_stt():
    """The primary live stop path must overlap the two independent GPU jobs.

    Diarization runs on the speaker GPU and STT runs on Parakeet's GPU. The
    diarization task must be created before STT is awaited, then joined only
    after the transcript is ready for speaker-label alignment.
    """
    src = _process_recording_source()
    launch_pos = src.find("diarize_task = asyncio.create_task(")
    stt_pos = src.find("stt_provider.transcribe(")
    join_pos = src.find("diar_segments = await diarize_task")

    assert launch_pos != -1, "concurrent diarization launch not found"
    assert stt_pos != -1, "STT call not found"
    assert join_pos != -1, "diarization join not found"
    assert launch_pos < stt_pos < join_pos, (
        "process_recording must launch diarization, run STT concurrently, "
        "then await diarization before merging speakers"
    )


def test_process_recording_identifies_speakers_before_indexing():
    src = _process_recording_source()

    # The actual call sites (not the imports, which lack the trailing "(").
    identify_pos = src.find("identify_speakers(")
    index_pos = src.find("index_session(")

    assert identify_pos != -1, "identify_speakers() call not found in process_recording"
    assert index_pos != -1, "index_session() call not found in process_recording"
    assert identify_pos < index_pos, (
        "process_recording must call identify_speakers() BEFORE index_session() "
        "so the embedded transcript carries real speaker names (v3.29.2 fix)"
    )


def test_process_recording_indexes_from_diarized_segments():
    """The index transcript is built speaker-aware from transcript_diarized
    segments ('{speaker}: {text}'), not from the label-less transcript_simple
    — same shape as reprocess Stage-5.9."""
    src = _process_recording_source()
    # The speaker-aware join lives right before the index_session call.
    index_pos = src.find("index_session(")
    window = src[max(0, index_pos - 800):index_pos]
    assert "transcript_diarized" in window, (
        "index transcript should be built from transcript_diarized segments"
    )
    assert "speaker" in window.lower(), (
        "index transcript should prefix each line with the segment speaker"
    )


def test_process_recording_summarizes_after_identify_and_normalize():
    """v3.34.0 (audit finding #4): the record->stop summary must be generated
    AFTER identify_speakers + normalize_session_speaker_labels, via the shared
    attributed-prompt path (api.uploads._summarize_session builds
    "Name: utterance" lines from the diarized segments) — NOT the legacy
    flat-text unified_agent_service.analyze_meeting call, which ran before
    identification and made the model guess who said what."""
    src = _process_recording_source()

    identify_pos = src.find("identify_speakers(")
    normalize_pos = src.find("normalize_session_speaker_labels(")
    summarize_pos = src.find("_summarize_session(")
    index_pos = src.find("index_session(")

    assert identify_pos != -1, "identify_speakers() call not found in process_recording"
    assert normalize_pos != -1, (
        "normalize_session_speaker_labels() call not found in process_recording"
    )
    assert summarize_pos != -1, "_summarize_session() call not found in process_recording"
    assert index_pos != -1, "index_session() call not found in process_recording"

    assert identify_pos < summarize_pos, (
        "identify_speakers() must run BEFORE the summary so matched voices "
        "carry real names into the prompt"
    )
    assert normalize_pos < summarize_pos, (
        "normalize_session_speaker_labels() must run BEFORE the summary so "
        "unmatched voices read 'Speaker N', never raw SPEAKER_xx"
    )
    assert summarize_pos < index_pos, (
        "index_session() stays last so Qdrant embeds the final labels + summary"
    )

    # The legacy flat-transcript summarizer (and its stale legacy
    # model-name log string) must not come back.
    assert "analyze_meeting" not in src, (
        "process_recording must not summarize via the legacy flat-text "
        "unified_agent_service.analyze_meeting path"
    )
    assert "granite" not in src.lower(), (
        "stale legacy model-name strings must stay purged from process_recording"
    )
