"""Pin the Knowledge Graph node-id parser.

`parse_node_id` is the load-bearing bridge between Brigade's bare graph nodes
(which carry only the canonical id) and the Postgres rows that hold the human
content. If the id grammar drifts, hydration silently stops and the graph
reverts to ugly ids — so lock the grammar here.
"""
from services.kg_hydrate import parse_node_id


def test_first_class_ids():
    assert parse_node_id("meeting_ops_speaker_7") == ("speaker", 7, None)
    assert parse_node_id("meeting_ops_meeting_102") == ("meeting", 102, None)
    assert parse_node_id("meeting_ops_action_116") == ("action", 116, None)


def test_indexed_ids():
    # topics/decisions are <parent_session_pk>_<idx> (JSON-in-final_summary).
    assert parse_node_id("meeting_ops_topic_157_1") == ("topic", 157, 1)
    assert parse_node_id("meeting_ops_decision_111_0") == ("decision", 111, 0)


def test_non_meetingops_ids_are_ignored():
    # Foreign namespaces (other apps writing the shared graph) must NOT hydrate
    # — that's how cross-tenant nodes get dropped by _scope_and_cap.
    assert parse_node_id("brigade_other_5") == (None, None, None)
    assert parse_node_id("person_42") == (None, None, None)
    assert parse_node_id("") == (None, None, None)
    assert parse_node_id(None) == (None, None, None)


def test_unknown_kind_is_ignored():
    assert parse_node_id("meeting_ops_widget_9") == (None, None, None)


def test_trailing_garbage_is_rejected():
    assert parse_node_id("meeting_ops_meeting_102_extra_5") == (None, None, None)
    assert parse_node_id("meeting_ops_speaker_7x") == (None, None, None)
