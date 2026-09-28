"""Unit tests for backend/utils/filename_parser.py."""

from datetime import date, time

import pytest

from utils.filename_parser import ParsedFilename, parse_filename


# ---------------------------------------------------------------------------
# Pattern 1: notes__ / downloads__ Mac Notes + Voice Memos export.
# Real-world filenames from the 528-file archive Aaron is importing.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,exp_source,exp_date,exp_time,exp_title",
    [
        (
            "notes__2024-11-05_233322__Call with Jason Allen.m4a",
            "notes",
            date(2024, 11, 5),
            time(23, 33, 22),
            "Call with Jason Allen",
        ),
        (
            "notes__2024-11-14_183247__Call with John Rahaghi.m4a",
            "notes",
            date(2024, 11, 14),
            time(18, 32, 47),
            "Call with John Rahaghi",
        ),
        (
            "downloads__2026-05-16_212900__Call with Shafen Khan.m4a",
            "downloads",
            date(2026, 5, 16),
            time(21, 29, 0),
            "Call with Shafen Khan",
        ),
        (
            "downloads__2026-05-14_161900__Legacy25 Capital.m4a",
            "downloads",
            date(2026, 5, 14),
            time(16, 19, 0),
            "Legacy25 Capital",
        ),
    ],
)
def test_pattern1_real_world(filename, exp_source, exp_date, exp_time, exp_title):
    p = parse_filename(filename)
    assert p.confidence == 1.0
    assert p.source == exp_source
    assert p.meeting_date == exp_date
    assert p.meeting_time == exp_time
    assert p.title == exp_title
    assert p.raw_filename == filename


def test_pattern1_title_with_parentheses():
    """Voice Memos sometimes save with parens in the user-typed name."""
    p = parse_filename("notes__2025-03-04_091500__Doug (Crash).m4a")
    assert p.confidence == 1.0
    assert p.source == "notes"
    assert p.meeting_date == date(2025, 3, 4)
    assert p.meeting_time == time(9, 15, 0)
    assert p.title == "Doug (Crash)"


def test_pattern1_mp3_extension():
    """Two of the 528 archive files are mp3, not m4a."""
    p = parse_filename("notes__2024-09-12_103045__Random thought.mp3")
    assert p.confidence == 1.0
    assert p.source == "notes"
    assert p.meeting_date == date(2024, 9, 12)
    assert p.meeting_time == time(10, 30, 45)
    assert p.title == "Random thought"


# ---------------------------------------------------------------------------
# Pattern 2: ISO prefix + " - " separator.
# ---------------------------------------------------------------------------

def test_pattern2_iso_prefix_dash():
    p = parse_filename("2024-03-15 - Jane Smith.txt")
    assert p.confidence == 0.9
    assert p.source == "generic"
    assert p.meeting_date == date(2024, 3, 15)
    assert p.meeting_time is None
    assert p.title == "Jane Smith"


def test_pattern2_with_audio_extension():
    p = parse_filename("2024-03-15 - Q1 planning sync.m4a")
    assert p.confidence == 0.9
    assert p.meeting_date == date(2024, 3, 15)
    assert p.title == "Q1 planning sync"


# ---------------------------------------------------------------------------
# Pattern 3: ISO prefix + loose separator (underscore or single space).
# ---------------------------------------------------------------------------

def test_pattern3_iso_prefix_underscore():
    p = parse_filename("2024-03-15_Jane Smith.txt")
    assert p.confidence == 0.8
    assert p.source == "generic"
    assert p.meeting_date == date(2024, 3, 15)
    assert p.title == "Jane Smith"


def test_pattern3_iso_prefix_space():
    p = parse_filename("2024-03-15 Jane Smith Q1 sync.m4a")
    assert p.confidence == 0.8
    assert p.meeting_date == date(2024, 3, 15)
    assert p.title == "Jane Smith Q1 sync"


# ---------------------------------------------------------------------------
# Pattern 4: US date suffix (M-D-YY or M-D-YYYY).
# ---------------------------------------------------------------------------

def test_pattern4_us_date_short_year():
    p = parse_filename("Jane Smith 3-15-24.txt")
    assert p.confidence == 0.7
    assert p.source == "generic"
    assert p.meeting_date == date(2024, 3, 15)
    assert p.title == "Jane Smith"


def test_pattern4_us_date_full_year():
    p = parse_filename("Quarterly review 3-15-2024.txt")
    assert p.confidence == 0.7
    assert p.meeting_date == date(2024, 3, 15)
    assert p.title == "Quarterly review"


def test_pattern4_us_date_with_dash_separator():
    p = parse_filename("Jane Smith - 3-15-24.txt")
    assert p.confidence == 0.7
    assert p.meeting_date == date(2024, 3, 15)
    assert p.title == "Jane Smith"


# ---------------------------------------------------------------------------
# Pattern 5: ISO date buried anywhere in the basename.
# ---------------------------------------------------------------------------

def test_pattern5_iso_anywhere():
    p = parse_filename("zoom_2024-03-15_recording.mp4")
    assert p.confidence == 0.5
    assert p.source == "generic"
    assert p.meeting_date == date(2024, 3, 15)
    # Title strips the matched ISO date, leaving the surrounding words.
    assert "zoom" in (p.title or "").lower()


# ---------------------------------------------------------------------------
# No match + edge cases.
# ---------------------------------------------------------------------------

def test_no_match_returns_zero_confidence():
    p = parse_filename("Just a random title.m4a")
    assert p.confidence == 0.0
    assert p.meeting_date is None
    assert p.meeting_time is None
    assert p.source is None
    # Title still gets best-effort cleanup for upload-time fallback.
    assert p.title == "Just a random title"


def test_empty_filename():
    p = parse_filename("")
    assert p.confidence == 0.0
    assert p.meeting_date is None
    assert p.meeting_time is None
    assert p.title is None


def test_extension_only():
    p = parse_filename(".m4a")
    assert p.confidence == 0.0
    assert p.meeting_date is None


def test_malformed_date_in_pattern1_shape_matches_but_date_is_none():
    """Pattern 1 shape can match while the calendar date is invalid.

    The shape regex doesn't second-guess month / day ranges, so a 99-month
    string still produces a confident parse for source + time + title.
    What the parser does NOT do is invent a wrong date — meeting_date
    comes back as None so the caller can detect the problem instead of
    storing 2024-09-15 silently.
    """
    p = parse_filename("notes__2024-99-15_120000__Bad date.m4a")
    assert p.confidence == 1.0
    assert p.source == "notes"
    assert p.meeting_date is None
    assert p.title == "Bad date"


def test_malformed_time_in_pattern1():
    """Pattern 1 with HHMMSS = 999999 — date parses, time fails safely."""
    p = parse_filename("notes__2024-11-05_999999__Bad time.m4a")
    # The time group is 6 digits but invalid time-of-day. parse_filename
    # returns confidence=1.0 (the shape matched) with meeting_time=None.
    assert p.confidence == 1.0
    assert p.meeting_date == date(2024, 11, 5)
    assert p.meeting_time is None
    assert p.title == "Bad time"


def test_pattern1_directory_prefix_stripped():
    """A path-y filename should still parse — we strip the directory."""
    p = parse_filename(
        "/Volumes/media/audio-from-notes-voicememos-2026-05-20/"
        "notes__2024-11-05_233322__Call with Jason Allen.m4a"
    )
    assert p.confidence == 1.0
    assert p.source == "notes"
    assert p.title == "Call with Jason Allen"


def test_returns_parsed_filename_instance():
    """Always returns a ParsedFilename, never raises."""
    for f in [None, "", "noext", "noext.m4a", "2024-13-99 bad.m4a"]:
        p = parse_filename(f or "")
        assert isinstance(p, ParsedFilename)
