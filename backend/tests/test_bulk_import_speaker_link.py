"""Tests for B-import.3 speaker auto-link from filename pattern.

Covers:

  1. extract_call_with_name   — happy path, ``(Crash)`` stripping, no-match, empties
  2. find_speaker_by_name_hint — exact case-insensitive match
  3. find_speaker_by_name_hint — fuzzy token_set_ratio (``Khan, Shafen`` ↔ ``Shafen Khan``)
  4. find_speaker_by_name_hint — no match above floor returns None
  5. find_speaker_by_name_hint — cross-org isolation
  6. Integration — ``Call with {Name}`` of an enrolled speaker creates a SpeakerSessionLink
  7. Integration — ``Call with {Name}`` where the name is unknown produces no link
  8. Integration — title NOT matching ``Call with X`` produces no link
"""

from __future__ import annotations

import uuid


import pytest


# ======================================================================
# Helpers
# ======================================================================


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    )
    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    )


def _seed_speaker(
    db, org_id: int, display_name: str,
):
    """Create an enrolled speaker for a given org."""
    _, _, _, _, _, SpeakerProfile, _ = _models()
    sp = SpeakerProfile(
        organization_id=org_id,
        display_name=display_name,
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


# ======================================================================
# Unit: extract_call_with_name
# ======================================================================


class TestExtractCallWithName:
    def test_call_with_jason_allen(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name("Call with Jason Allen") == "Jason Allen"

    def test_call_with_doug_crash_strips_parens(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name("Call with Doug (Crash)") == "Doug"

    def test_non_call_pattern_returns_none(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name("Mtg with John") is None

    def test_empty_string_returns_none(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name("") is None

    def test_none_input_returns_none(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name(None) is None

    def test_trailing_whitespace_is_stripped(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name("  Call with   Jason Allen  ") == "Jason Allen"

    def test_case_insensitive(self):
        from utils.filename_parser import extract_call_with_name
        assert extract_call_with_name("call with jason allen") == "jason allen"
        assert extract_call_with_name("CALL WITH Jason Allen") == "Jason Allen"


# ======================================================================
# Unit: find_speaker_by_name_hint
# ======================================================================


class TestFindSpeakerByNameHint:
    def test_exact_match(self, app):
        """Exact case-insensitive match returns the correct speaker."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, _ = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org = OrgModel(name=f"FH Exact {suffix}", slug=f"fh-exact-{suffix}", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
            sp = _seed_speaker(db, org.id, "Jason Allen")

            from services.speaker_service import find_speaker_by_name_hint
            result = find_speaker_by_name_hint(db, org.id, "Jason Allen")
            assert result is not None
            assert result.id == sp.id

            # Case-insensitive
            result = find_speaker_by_name_hint(db, org.id, "jason allen")
            assert result is not None
            assert result.id == sp.id
        finally:
            db.close()

    def test_fuzzy_khan_shafen(self, app):
        """token_set_ratio matches 'Khan, Shafen' to 'Shafen Khan'."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, _ = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org = OrgModel(name=f"FH Fuzzy {suffix}", slug=f"fh-fuzzy-{suffix}", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
            sp = _seed_speaker(db, org.id, "Shafen Khan")

            from services.speaker_service import find_speaker_by_name_hint
            result = find_speaker_by_name_hint(db, org.id, "Khan, Shafen")
            assert result is not None
            assert result.id == sp.id
        finally:
            db.close()

    def test_no_match_above_floor(self, app):
        """Returns None when the best fuzzy score is below the floor."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, _ = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org = OrgModel(name=f"FH NoMatch {suffix}", slug=f"fh-nomatch-{suffix}", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
            _seed_speaker(db, org.id, "Chris Mooney")

            from services.speaker_service import find_speaker_by_name_hint
            result = find_speaker_by_name_hint(db, org.id, "Bob Smith")
            assert result is None
        finally:
            db.close()

    def test_cross_org_isolation(self, app):
        """Speakers from org A are not visible when searching org B."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, _ = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org_a = OrgModel(name=f"OrgA {suffix}", slug=f"org-a-{suffix}", is_active=True)
            org_b = OrgModel(name=f"OrgB {suffix}", slug=f"org-b-{suffix}", is_active=True)
            db.add_all([org_a, org_b])
            db.commit()
            db.refresh(org_a)
            db.refresh(org_b)

            _seed_speaker(db, org_a.id, "Shafen Khan")

            from services.speaker_service import find_speaker_by_name_hint
            # Searching org B (which has no speakers) should return None
            result = find_speaker_by_name_hint(db, org_b.id, "Shafen Khan")
            assert result is None
        finally:
            db.close()

    def test_no_enrolled_speakers(self, app):
        """Returns None when the org has no speakers at all."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, _, _ = _models()
        db = SessionLocal()
        try:
            org = OrgModel(name="Empty Org", slug="empty-org-test", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)

            from services.speaker_service import find_speaker_by_name_hint
            result = find_speaker_by_name_hint(db, org.id, "Jason Allen")
            assert result is None
        finally:
            db.close()


# ======================================================================
# Integration: speaker auto-link from filename hint (Step 3.5 logic)
# ======================================================================
# These tests exercise the auto-link code directly — create a session,
# call extract_call_with_name + find_speaker_by_name_hint + SpeakerSessionLink,
# rather than going through the full _do_process_file pipeline (which has
# Garage / Parakeet / FK-resolution dependencies that belong in Docker).


def _make_session_and_link(
    db, org_id: int, title: str,
):
    """Execute Step 3.5 logic: parse title → find speaker → create link.

    Returns the created SpeakerSessionLink (or None if no link was made).
    """
    from services.speaker_service import find_speaker_by_name_hint
    from utils.filename_parser import extract_call_with_name

    name_hint = extract_call_with_name(title) if title else None
    if not name_hint:
        return None
    speaker = find_speaker_by_name_hint(db, org_id, name_hint)
    if not speaker:
        return None

    from database.models import RecordingSession, SpeakerSessionLink

    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        organization_id=org_id,
        status="processing",
    )
    db.add(session)
    db.flush()

    link = SpeakerSessionLink(
        speaker_id=speaker.id,
        session_id=session.id,
        organization_id=org_id,
        source="filename-hint",
        raw_label="HINT",
    )
    db.add(link)
    db.commit()
    return link


class TestIntegration:
    """Direct tests of Step 3.5 speaker auto-link logic."""

    def test_known_speaker_creates_link(self, app):
        """Call with Shafen Khan + enrolled speaker → SpeakerSessionLink created."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org = OrgModel(name=f"Int1 {suffix}", slug=f"int1-{suffix}", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
            _seed_speaker(db, org.id, "Shafen Khan")

            link = _make_session_and_link(db, org.id, "Call with Shafen Khan")
            assert link is not None
            assert link.source == "filename-hint"
            assert link.raw_label == "HINT"

            stored = db.get(SpeakerSessionLink, link.id)
            assert stored is not None
            assert stored.raw_label == "HINT"
        finally:
            db.close()

    def test_unknown_speaker_no_link(self, app):
        """Call with Name for a speaker not enrolled → no link."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org = OrgModel(name=f"Int2 {suffix}", slug=f"int2-{suffix}", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)

            link = _make_session_and_link(db, org.id, "Call with Shafen Khan")
            assert link is None

            count = db.query(SpeakerSessionLink).filter(
                SpeakerSessionLink.organization_id == org.id,
            ).count()
            assert count == 0
        finally:
            db.close()

    def test_non_call_pattern_no_link(self, app):
        """Title that doesn't match 'Call with X' → no link."""
        from auth.models import Organization as OrgModel
        _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink = _models()
        db = SessionLocal()
        try:
            suffix = uuid.uuid4().hex[:6]
            org = OrgModel(name=f"Int3 {suffix}", slug=f"int3-{suffix}", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
            _seed_speaker(db, org.id, "Shafen Khan")

            link = _make_session_and_link(db, org.id, "Quarterly Planning Sync")
            assert link is None

            count = db.query(SpeakerSessionLink).filter(
                SpeakerSessionLink.organization_id == org.id,
            ).count()
            assert count == 0
        finally:
            db.close()
