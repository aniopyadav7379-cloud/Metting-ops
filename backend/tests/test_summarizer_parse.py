"""Unit tests for the summarizer JSON extractor.

Qwen 3.6 emits prose followed by a fenced ```json``` block; older models
return bare JSON. The parser must handle either, and fall back gracefully
when the model returns plain prose so we still write *something* to the
session summary.
"""

from api.uploads import _parse_summarizer_json


def test_fenced_json_after_prose():
    text = """Here's the analysis you asked for.

**Executive Summary**
The team decided to ship Friday.

```json
{"executive": "Team ships Friday.", "decisions": ["Ship Friday"], "actions": [], "questions": [], "title": "Ship Decision"}
```
"""
    parsed = _parse_summarizer_json(text, fallback_title="fallback")
    assert parsed["title"] == "Ship Decision"
    assert parsed["decisions"] == ["Ship Friday"]


def test_bare_json():
    text = '{"executive":"Quick sync","decisions":[],"actions":[],"questions":[],"title":"Sync"}'
    parsed = _parse_summarizer_json(text, fallback_title="fallback")
    assert parsed["title"] == "Sync"


def test_fenced_without_lang_tag():
    text = """```
{"executive":"x","title":"Untagged Fence"}
```"""
    parsed = _parse_summarizer_json(text, fallback_title="fallback")
    assert parsed["title"] == "Untagged Fence"


def test_prose_only_falls_back_to_executive():
    text = "I could not produce structured output."
    parsed = _parse_summarizer_json(text, fallback_title="My Meeting")
    assert parsed["executive"].startswith("I could not")
    assert parsed["title"] == "My Meeting"
    assert parsed["decisions"] == []


def test_empty_text_falls_back():
    parsed = _parse_summarizer_json("", fallback_title="My Meeting")
    assert "Summary generation returned no content" in parsed["executive"]
    assert parsed["title"] == "My Meeting"


def test_first_valid_json_wins_over_invalid():
    """If the model emits a malformed JSON-ish fragment first then a valid
    fenced block, we should still find the fenced one."""
    text = """thinking... {oops not json}

```json
{"executive": "real", "title": "Real Title"}
```"""
    parsed = _parse_summarizer_json(text, fallback_title="fallback")
    assert parsed["title"] == "Real Title"
