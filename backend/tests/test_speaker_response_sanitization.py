"""Browser responses must never expose stored speaker biometrics."""

from copy import deepcopy


def test_sanitizer_removes_embeddings_recursively_without_mutating_source():
    from services.speaker_service import sanitize_diarized_for_response

    source = {
        "segments": [
            {
                "speaker": "Speaker 1",
                "text": "hello",
                "embedding": [0.1, 0.2],
                "nested": {
                    "embeddings": [[0.3, 0.4]],
                    "centroid_embedding": [0.5, 0.6],
                    "safe": "kept",
                },
            }
        ],
        "speaker_turns": [
            {"speaker": "SPEAKER_00", "embedding": [0.7, 0.8]}
        ],
        "model": "speaker-model",
    }
    before = deepcopy(source)

    result = sanitize_diarized_for_response(source)

    assert result == {
        "segments": [
            {
                "speaker": "Speaker 1",
                "text": "hello",
                "nested": {"safe": "kept"},
            }
        ],
        "model": "speaker-model",
    }
    assert source == before


def test_sanitizer_preserves_non_mapping_payloads():
    from services.speaker_service import sanitize_diarized_for_response

    assert sanitize_diarized_for_response(None) is None
    assert sanitize_diarized_for_response("plain transcript") == "plain transcript"
