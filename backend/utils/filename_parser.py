"""Filename → meeting metadata parser.

Pure-Python, no I/O. Used at upload time to pre-fill meeting_date /
meeting_time / title on new sessions, and exposed as an endpoint so the
frontend can preview the parsed values before submitting.

Patterns, in priority order:

1. Mac Notes + Voice Memos export:
       {notes|downloads}__YYYY-MM-DD_HHMMSS__{title}.{ext}
   Confidence 1.0. All 528 files in Aaron's 2024-09 → 2026-05 audio
   archive match this shape.

2. ISO date prefix with dash separator:
       YYYY-MM-DD - Title.ext
   Confidence 0.9.

3. ISO date prefix, loose separator (underscore or space):
       YYYY-MM-DD_Title.ext
       YYYY-MM-DD Title.ext
   Confidence 0.8.

4. US date suffix with M-D-YY or M-D-YYYY:
       Title - 3-15-24.ext
       Title 3-15-2024.ext
   Confidence 0.7.

5. ISO date anywhere in the basename, no positional contract:
       Anything-with-2024-03-15-buried-inside.ext
   Confidence 0.5.

No match → confidence 0.0, title = best-effort stem cleanup.

The parser is the single source of truth for both the upload pipeline
and the /api/recordings/parse-filename endpoint. The TypeScript twin in
frontend/src/utils/filenameParser.ts mirrors the same five patterns so
the client preview matches what the server stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Optional


@dataclass
class ParsedFilename:
    """Result of parse_filename. Confidence reflects which pattern matched."""

    title: Optional[str]
    meeting_date: Optional[date]
    meeting_time: Optional[time]
    source: Optional[str]
    raw_filename: str
    confidence: float


# Pattern 1: Mac Notes + Voice Memos export.
# {notes|downloads}__YYYY-MM-DD_HHMMSS__{title}.{ext}
_PATTERN_NOTES_EXPORT = re.compile(
    r"^(?P<source>notes|downloads)"
    r"__(?P<date>\d{4}-\d{2}-\d{2})"
    r"_(?P<time>\d{6})"
    r"__(?P<title>.+?)"
    r"\.(?P<ext>[A-Za-z0-9]{1,8})$"
)

# Pattern 2: ISO prefix + " - " separator.
_PATTERN_ISO_PREFIX_DASH = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s*-\s*(?P<title>.+?)$"
)

# Pattern 3: ISO prefix with underscore or single space.
_PATTERN_ISO_PREFIX_LOOSE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ _](?P<title>.+?)$"
)

# Pattern 4: US date suffix. Accepts M-D-YY / MM-DD-YY / M-D-YYYY etc.
# Title sits before the date, with optional " - " or whitespace.
_PATTERN_US_DATE_SUFFIX = re.compile(
    r"^(?P<title>.+?)"
    r"(?:\s*[-_]\s*|\s+)"
    r"(?P<month>\d{1,2})[-/](?P<day>\d{1,2})[-/](?P<year>\d{2}|\d{4})"
    r"$"
)

# Pattern 5: ISO date anywhere in the basename.
_PATTERN_ISO_ANYWHERE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _safe_time(hh: int, mm: int, ss: int) -> Optional[time]:
    try:
        return time(hh, mm, ss)
    except (TypeError, ValueError):
        return None


def _normalize_title(raw: str) -> str:
    """Tidy a raw title fragment for display.

    - collapse runs of whitespace / underscores
    - strip leading/trailing punctuation we never want in a title
    - cap at 200 chars (matches the DB column ceiling)
    """
    if not raw:
        return ""
    cleaned = raw.replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -._")
    return cleaned[:200]


def _expand_two_digit_year(year_raw: str) -> int:
    """Two-digit year → four-digit. ≥70 maps to 19YY, <70 to 20YY.

    A pragmatic split that handles meeting filenames going back into the
    late 90s without mis-bucketing anything from this decade.
    """
    n = int(year_raw)
    if len(year_raw) == 2:
        return 1900 + n if n >= 70 else 2000 + n
    return n


def parse_filename(filename: str) -> ParsedFilename:
    """Parse a filename into title / meeting_date / meeting_time / source.

    Always returns a ParsedFilename — never raises. confidence=0.0 means
    no pattern matched and the caller should treat all fields as
    untrusted.
    """
    raw = filename or ""
    # Strip directory, in case a caller passes a path.
    basename = Path(raw).name if raw else ""
    # Pattern 1 is the only one that owns the full filename including
    # the extension (the {ext} group is part of the regex). Try it first.
    m = _PATTERN_NOTES_EXPORT.match(basename)
    if m:
        d = _safe_date(
            int(m.group("date")[0:4]),
            int(m.group("date")[5:7]),
            int(m.group("date")[8:10]),
        )
        t_raw = m.group("time")
        t = _safe_time(int(t_raw[0:2]), int(t_raw[2:4]), int(t_raw[4:6]))
        return ParsedFilename(
            title=_normalize_title(m.group("title")),
            meeting_date=d,
            meeting_time=t,
            source=m.group("source"),
            raw_filename=raw,
            confidence=1.0,
        )

    # Patterns 2-5 work on the stem (no extension). Strip the last
    # extension only — preserving any dots in the title itself.
    stem = Path(basename).stem if basename else ""

    m = _PATTERN_ISO_PREFIX_DASH.match(stem)
    if m:
        d = _safe_date(
            int(m.group("date")[0:4]),
            int(m.group("date")[5:7]),
            int(m.group("date")[8:10]),
        )
        if d:
            return ParsedFilename(
                title=_normalize_title(m.group("title")),
                meeting_date=d,
                meeting_time=None,
                source="generic",
                raw_filename=raw,
                confidence=0.9,
            )

    m = _PATTERN_ISO_PREFIX_LOOSE.match(stem)
    if m:
        d = _safe_date(
            int(m.group("date")[0:4]),
            int(m.group("date")[5:7]),
            int(m.group("date")[8:10]),
        )
        if d:
            return ParsedFilename(
                title=_normalize_title(m.group("title")),
                meeting_date=d,
                meeting_time=None,
                source="generic",
                raw_filename=raw,
                confidence=0.8,
            )

    m = _PATTERN_US_DATE_SUFFIX.match(stem)
    if m:
        d = _safe_date(
            _expand_two_digit_year(m.group("year")),
            int(m.group("month")),
            int(m.group("day")),
        )
        if d:
            return ParsedFilename(
                title=_normalize_title(m.group("title")),
                meeting_date=d,
                meeting_time=None,
                source="generic",
                raw_filename=raw,
                confidence=0.7,
            )

    m = _PATTERN_ISO_ANYWHERE.search(stem)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            # Strip the matched date out of the stem to recover a title.
            without_date = (stem[: m.start()] + stem[m.end():]).strip(" -_.")
            title = _normalize_title(without_date) or _normalize_title(stem)
            return ParsedFilename(
                title=title,
                meeting_date=d,
                meeting_time=None,
                source="generic",
                raw_filename=raw,
                confidence=0.5,
            )

    # No pattern matched. Return a best-effort title (cleaned stem) and
    # let the caller decide whether to fall back to mtime or a default.
    fallback_title = _normalize_title(stem) or None
    return ParsedFilename(
        title=fallback_title,
        meeting_date=None,
        meeting_time=None,
        source=None,
        raw_filename=raw,
        confidence=0.0,
    )


# Regex for the "Call with {Name}" pattern used by speaker auto-link (B-import.3).
_CALL_WITH_RE = re.compile(r"^\s*Call\s+with\s+(.+?)\s*$", re.IGNORECASE)


def extract_call_with_name(title: Optional[str]) -> Optional[str]:
    """If *title* matches ``Call with {Name}`` (case-insensitive), return the
    cleaned name. Handles parenthetical suffixes like ``Doug (Crash)`` by
    stripping the parenthetical for matching purposes, returning ``Doug``.
    Returns ``None`` for any other format or a falsy input.
    """
    if not title:
        return None
    m = _CALL_WITH_RE.match(title)
    if not m:
        return None
    name = m.group(1).strip()
    # Strip a trailing parenthetical like " (Crash)" so 'Doug (Crash)'
    # matches an enrolled speaker whose display_name is 'Doug'.
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    return name or None


if __name__ == "__main__":
    samples = [
        # Pattern 1 — real-world Mac Notes + Voice Memos export.
        "notes__2024-11-05_233322__Call with Jason Allen.m4a",
        "notes__2024-11-14_183247__Call with John Rahaghi.m4a",
        "downloads__2026-05-16_212900__Call with Shafen Khan.m4a",
        "downloads__2026-05-14_161900__Legacy25 Capital.m4a",
        # Pattern 1 — title with parentheses.
        "notes__2025-03-04_091500__Doug (Crash).m4a",
        # Pattern 2 — ISO prefix with " - ".
        "2024-03-15 - Jane Smith.txt",
        # Pattern 3 — ISO prefix loose.
        "2024-03-15_Jane Smith.txt",
        "2024-03-15 Jane Smith Q1 sync.m4a",
        # Pattern 4 — US date suffix.
        "Jane Smith - 3-15-24.txt",
        "Quarterly review 3-15-2024.txt",
        # Pattern 5 — ISO date buried.
        "zoom_2024-03-15_recording.mp4",
        # No match.
        "Just a random title.m4a",
        "",
    ]
    for f in samples:
        p = parse_filename(f)
        print(
            f"{p.confidence:.1f}  date={p.meeting_date}  time={p.meeting_time}  "
            f"src={p.source!s:>10}  title={p.title!r}  file={f!r}"
        )
