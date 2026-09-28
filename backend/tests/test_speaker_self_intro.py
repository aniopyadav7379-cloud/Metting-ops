"""Unit tests for the self-introduction name extractor.

Pure module — no DB / client needed; imports ``extract_self_intro_names``
directly. The centerpiece is the false-positive battery: a conservative
extractor that invents a wrong name is worse than one that misses a real
intro (the user just types the name). These tests lock that contract.
"""
from __future__ import annotations

from services.speaker_self_intro import extract_self_intro_names


def _names(texts):
    return [s["name"] for s in extract_self_intro_names(texts)]


# --------------------------------------------------------------------------
# POSITIVE — real self-introductions are extracted.
# --------------------------------------------------------------------------


def test_greeting_im_extracts_name():
    out = extract_self_intro_names(["Hi, I'm John"])
    assert [s["name"] for s in out] == ["John"]
    # Evidence carries the source clause for the audit tooltip.
    assert "John" in out[0]["evidence"]


def test_bare_im_without_greeting():
    assert _names(["I'm Sarah and I'll take notes today."]) == ["Sarah"]


def test_this_is_frame():
    assert _names(["This is John."]) == ["John"]


def test_my_name_is_two_tokens():
    assert _names(["My name is John Smith, nice to meet you."]) == ["John Smith"]


def test_name_here_frame():
    assert _names(["John here."]) == ["John"]


def test_speaking_with_frame():
    assert _names(["You're speaking with Sarah from accounting."]) == ["Sarah"]


def test_hyphenated_name():
    assert _names(["Hello, I am Anne-Marie"]) == ["Anne-Marie"]


def test_apostrophe_name():
    assert _names(["Hi, I'm O'Brien"]) == ["O'Brien"]


def test_my_name_apostrophe_s_contraction():
    # "my name's Dave"
    assert _names(["Hey, my name's Dave."]) == ["Dave"]


def test_its_frame():
    assert _names(["It's Maria, calling back about the invoice."]) == ["Maria"]


def test_intro_in_a_later_segment_of_the_list():
    # The intro can land in any of the speaker's own segments, not just seg 0.
    texts = [
        "Okay so where were we.",
        "Right, the budget.",
        "Oh sorry, I'm Michael by the way.",
    ]
    assert _names(texts) == ["Michael"]


def test_three_token_name():
    assert _names(["My name is Mary Anne Lee."]) == ["Mary Anne Lee"]


# --------------------------------------------------------------------------
# NEGATIVE / false-positive guards — the centerpiece. ALL return [].
# --------------------------------------------------------------------------


def test_im_happy_is_not_a_name():
    assert extract_self_intro_names(["I'm happy"]) == []


def test_im_going_to_is_not_a_name():
    assert extract_self_intro_names(["I'm going to grab coffee"]) == []


def test_im_not_sure_is_not_a_name():
    assert extract_self_intro_names(["I'm not sure about that"]) == []


def test_im_just_saying_is_not_a_name():
    assert extract_self_intro_names(["I'm just saying we should wait"]) == []


def test_im_sorry_is_not_a_name():
    assert extract_self_intro_names(["I'm sorry, can you repeat that?"]) == []


def test_this_is_great_is_not_a_name():
    assert extract_self_intro_names(["This is great"]) == []


def test_this_is_the_plan_is_not_a_name():
    assert extract_self_intro_names(["This is the plan for next quarter"]) == []


def test_my_name_is_on_the_badge_is_not_a_name():
    # "on" is lowercased -> not a Titlecase token -> no capture.
    assert extract_self_intro_names(["My name is on the badge"]) == []


def test_im_ceo_acronym_rejected():
    assert extract_self_intro_names(["I'm CEO of the company"]) == []


def test_im_ok_rejected():
    assert extract_self_intro_names(["I'm OK"]) == []


def test_pronoun_after_frame_rejected():
    # "this is he" / "I'm I" must never produce a name.
    assert extract_self_intro_names(["This is He", "I'm I"]) == []


def test_mid_sentence_this_is_does_not_fire():
    # "all this is great" — the clause does not START with "this is",
    # so the clause-anchored frame must not fire.
    assert extract_self_intro_names(["Honestly all this is great work"]) == []


def test_weekday_after_frame_rejected():
    assert extract_self_intro_names(["This is Monday's agenda"]) == []


def test_lowercase_only_transcript_yields_nothing():
    # Some STT modes emit lowercase-only text. Fail closed (no false
    # positives) rather than relaxing the capital requirement.
    assert extract_self_intro_names(["hi, i'm john"]) == []


# --------------------------------------------------------------------------
# ROBUSTNESS — dedupe, cap, empty, possessive.
# --------------------------------------------------------------------------


def test_dedupe_same_name_across_segments():
    texts = ["Hi, I'm John.", "Anyway, John here, picking back up."]
    assert _names(texts) == ["John"]


def test_cap_at_three_distinct_names():
    # Five distinct intros, only the first three (by appearance) surface.
    texts = [
        "I'm Aaron.",
        "This is Bethany.",
        "My name is Carlos.",
        "Hi, I'm Diana.",
        "It's Edward.",
    ]
    out = _names(texts)
    assert len(out) == 3
    assert out == ["Aaron", "Bethany", "Carlos"]


def test_empty_input_returns_empty():
    assert extract_self_intro_names([]) == []
    assert extract_self_intro_names(["", "   ", None]) == []  # type: ignore[list-item]


def test_possessive_is_stripped():
    # Pinned behavior: "this is John's deck" -> "John" (possessive dropped).
    assert _names(["This is John's deck for the review."]) == ["John"]


def test_ordering_prefers_first_appearance():
    texts = ["This is Zoe.", "Hi, I'm Adam."]
    # First-seen order, not alphabetical.
    assert _names(texts) == ["Zoe", "Adam"]
