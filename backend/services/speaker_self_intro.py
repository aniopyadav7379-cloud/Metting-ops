"""Self-introduction name extractor (suggestion-only, deterministic).

When a diarized speaker cluster is NOT matched to a voiceprint
(``SpeakerSessionLink.speaker_id IS NULL``), we scan THAT speaker's OWN
utterances for a first-person self-introduction ("Hi, I'm John", "this is
John", "my name is John Smith", "John here", "you're speaking with Sarah")
and surface the extracted name as a ONE-CLICK SUGGESTION in the
speaker-naming UI.

This module is the pure, side-effect-free core of that feature:
``extract_self_intro_names(texts)`` takes a list of the speaker's own
segment texts and returns at most three ordered, deduped
``{name, evidence}`` dicts. It does NO I/O, touches NO database, and imports
nothing from ``api/`` — so it is fully unit-testable in isolation and cheap
to call on-read.

Design rules (see the architect's contract):
  * SUGGESTION ONLY — this module never applies a name, never writes; the
    caller renders the result as a chip the user confirms.
  * OWN UTTERANCES ONLY — the caller feeds exclusively the one cluster's
    segment texts (``_segments_for_label``). "I'm John" spoken by a
    DIFFERENT cluster is structurally unreachable.
  * CONSERVATIVE > comprehensive — a false positive (inventing a wrong
    name) is far worse than a miss (the user just types the name). Every
    captured name token must be a Titlecase proper-noun shape AND survive a
    denylist of verb/adjective/adverb/pronoun continuations; intro frames
    are anchored at a sentence/clause boundary. Missing a real intro on a
    lowercase-only transcript is acceptable (fail closed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# How many distinct suggestions we ever surface for one cluster.
MAX_SUGGESTIONS = 3

# Single-token name shape: a leading capital then proper-noun-ish remainder.
# Allows O'Brien / Anne-Marie / D'Angelo. The leading-capital requirement is
# the load-bearing false-positive defense (kills "i'm happy", "i'm going").
_NAME_TOKEN = r"[A-Z][a-zA-Z'’-]{1,}"
# 1 to 3 such tokens, space-separated ("John", "John Smith", "Mary Anne Lee").
_NAME = rf"{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}}"

# Lowercased first-token denylist: the verb/adjective/adverb/filler
# continuations that most often follow "I'm" / "this is" and would otherwise
# masquerade as a name once the transcript happens to capitalize them
# (sentence start). Compared case-insensitively against the captured token.
_DENYLIST: frozenset[str] = frozenset(
    {
        # adjectives / states after "I'm ..."
        "happy", "sad", "sorry", "fine", "good", "great", "ok", "okay",
        "sure", "glad", "excited", "afraid", "ready", "late", "early",
        "certain", "positive", "confident", "curious", "aware", "unsure",
        "right", "wrong", "kidding", "joking",
        # adverbs / hedges
        "just", "really", "very", "so", "too", "still", "now", "back",
        "first", "last",
        # verb continuations ("I'm going to", "I'm trying to")
        "going", "gonna", "trying", "thinking", "looking", "working",
        "calling", "speaking", "wondering", "hoping", "talking", "getting",
        "doing", "saying", "telling", "asking", "running",
        # negations / determiners / prepositions / pronouns
        "not", "no", "the", "a", "an", "in", "on", "at", "with", "about",
        "done", "here", "there", "your", "my", "our", "their", "his",
        "her", "all", "of", "for", "to", "and",
        # pronouns (subject) that could land at clause start
        "he", "she", "they", "we", "you", "it", "i",
        # fillers
        "um", "uh", "well", "like", "yeah", "yes", "hmm",
        # bare honorifics / titles: a clause-split on "Dr." / "Mr." strands the
        # honorific alone ("I'm Dr. Smith" -> clause "I'm Dr"); suggesting the
        # title is worse than no suggestion, so reject a lone leading title.
        "dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir",
        "madam", "madame", "dame", "lord", "lady", "rev", "fr", "st",
    }
)

# Weekdays + months: an "I'm Monday" / "this is March" is a date, not a name.
_TIME_WORDS: frozenset[str] = frozenset(
    {
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday",
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
    }
)

# Confidence rank only used for stable ordering of equally-early candidates;
# never surfaced as a number. Higher = stronger intro frame.
_HIGH = 2
_MEDIUM = 1

# --- compiled intro frames -------------------------------------------------
# Each pattern captures the NAME in group "name". Patterns are matched against
# a single clause (already split on sentence + comma boundaries), so '^' means
# "clause start". re.IGNORECASE is applied to the FRAME words only — the NAME
# subpattern keeps its explicit [A-Z] capital requirement because the regex is
# compiled with re.IGNORECASE, so we re-validate the captured token's casing in
# Python (see _is_titlecase) rather than relying on the pattern alone.

# High-confidence frames.
_RE_GREETING_IAM = re.compile(
    rf"^(?:hi|hello|hey)[,!.\s]+(?:i\s*am|i'?m)\s+(?P<name>{_NAME})",
    re.IGNORECASE,
)
_RE_MY_NAME_IS = re.compile(
    rf"\bmy\s+name(?:\s+is|'?s)\s+(?P<name>{_NAME})",
    re.IGNORECASE,
)
_RE_SPEAKING_WITH = re.compile(
    rf"\b(?:you're|you\s+are|you've\s+got|you\s+have)?\s*(?:speaking\s+with|got)\s+(?P<name>{_NAME})",
    re.IGNORECASE,
)

# Medium-confidence frames.
_RE_BARE_IAM = re.compile(
    rf"^(?:i\s*am|i'?m)\s+(?P<name>{_NAME})",
    re.IGNORECASE,
)
_RE_THIS_IS = re.compile(
    rf"^(?:this\s+is|it'?s|it\s+is)\s+(?P<name>{_NAME})",
    re.IGNORECASE,
)
_RE_NAME_HERE = re.compile(
    rf"^(?P<name>{_NAME})\s+here[,.!]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SelfIntroName:
    """A single extracted self-intro candidate.

    ``name`` is the cleaned proper-noun (1-3 Titlecase tokens). ``evidence``
    is the trimmed clause it was found in — surfaced as the FE tooltip so the
    suggestion is human-auditable.
    """

    name: str
    evidence: str = ""


def _is_titlecase(token: str) -> bool:
    """A name token must be Titlecase: each ``'``/``-`` separated part has one
    leading capital and the rest lowercase (so O'Brien, Anne-Marie and
    D'Angelo pass, but OK / CEO / I / any ALL-CAPS acronym fail — the regex's
    re.IGNORECASE would otherwise let those through).
    """
    if not token or not token[0].isupper():
        return False
    parts = re.split(r"['’-]", token)
    saw_alpha = False
    for part in parts:
        if not part:
            continue  # leading/trailing/double separator — ignore
        if not part[0].isupper():
            return False
        # Each part: leading capital, remainder lowercase. Kills interior
        # capital runs ("McAdam" intercaps are rejected — conservative; a user
        # can type those) and ALL-CAPS parts.
        if part[1:] and not part[1:].islower():
            return False
        saw_alpha = True
    return saw_alpha


def _strip_possessive(token: str) -> str:
    """Drop a trailing possessive: "John's" / "John’s" -> "John"."""
    return re.sub(r"['’]s$", "", token)


def _clean_name(raw: str) -> str | None:
    """Validate + normalize a captured NAME span. The span is captured
    greedily (1-3 tokens) by the frame regex, so it can trail into non-name
    words ("Michael by the", "Sarah and"). We TRUNCATE at the first token that
    isn't a valid Titlecase, non-denylisted name token rather than rejecting
    the whole match — but the FIRST token must always pass every guard, so
    "I'm happy" / "I'm going" still yield nothing.

    Returns the cleaned 1-3 token name, or None if the first token fails.
    """
    tokens = [t for t in raw.strip().split() if t]
    if not tokens:
        return None

    cleaned: list[str] = []
    for tok in tokens:
        bare = _strip_possessive(tok.strip(".,!?;:"))
        low = bare.lower().strip("'’-")
        if not bare or low in _DENYLIST or low in _TIME_WORDS or not _is_titlecase(bare):
            break  # truncate the name here
        cleaned.append(bare)
        if len(cleaned) == 3:
            break

    if not cleaned:
        return None

    name = " ".join(cleaned)
    # Total length floor + reject every-token-single-char (e.g. initials only).
    if len(name.replace(" ", "")) < 2:
        return None
    if all(len(re.sub(r"['’-]", "", t)) <= 1 for t in cleaned):
        return None
    return name


def _split_clauses(text: str) -> list[str]:
    """Split an utterance into clauses for clause-anchored matching.

    Split first on sentence terminators (. ? ! and newlines), then further on
    commas — so a mid-sentence "all this is great" cannot fire the
    clause-start "this is" frame, while "Hi, I'm John" still exposes "I'm
    John" as its own clause.
    """
    out: list[str] = []
    for sentence in re.split(r"[.?!\n]+", text):
        for clause in sentence.split(","):
            clause = clause.strip()
            if clause:
                out.append(clause)
    return out


def _frames_for_clause(clause: str) -> list[tuple[int, str]]:
    """Run every intro frame against one clause. Returns a list of
    (confidence, raw_name_span) for each frame that fired with a valid-shaped
    capture. Validation/cleaning happens in the caller."""
    hits: list[tuple[int, str]] = []

    m = _RE_GREETING_IAM.search(clause)
    if m:
        hits.append((_HIGH, m.group("name")))

    m = _RE_MY_NAME_IS.search(clause)
    if m:
        hits.append((_HIGH, m.group("name")))

    m = _RE_SPEAKING_WITH.search(clause)
    if m:
        hits.append((_HIGH, m.group("name")))

    m = _RE_BARE_IAM.match(clause)
    if m:
        hits.append((_MEDIUM, m.group("name")))

    m = _RE_THIS_IS.match(clause)
    if m:
        # The greedy NAME capture is truncated to valid name tokens in
        # _clean_name, so "this is great news" yields nothing ("great" is
        # lowercase -> rejected) while "this is John from sales" -> "John".
        hits.append((_MEDIUM, m.group("name")))

    m = _RE_NAME_HERE.match(clause)
    if m:
        hits.append((_MEDIUM, m.group("name")))

    return hits


def extract_self_intro_names(texts: list[str]) -> list[dict]:
    """Extract self-introduction name suggestions from ONE speaker's own
    utterances.

    Parameters
    ----------
    texts:
        The speaker's own segment texts (one per diarized segment). The caller
        MUST pass only the target cluster's segments — this function never sees
        the full transcript, which is both the privacy and the accuracy rule.

    Returns
    -------
    list[dict]
        At most ``MAX_SUGGESTIONS`` ordered, case-insensitively deduped
        ``{"name": str, "evidence": str}`` dicts (first-seen order, ties broken
        by stronger intro frame). Empty list when nothing passes the
        conservative guards (e.g. "I'm happy", lowercase-only transcripts).
    """
    if not texts:
        return []

    # Collect candidates preserving first-appearance order; keep the strongest
    # frame + its evidence for each distinct (case-insensitive) name.
    order: list[str] = []  # lowercased keys, first-seen order
    best: dict[str, tuple[int, str, str]] = {}  # key -> (confidence, name, evidence)

    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        for clause in _split_clauses(text):
            for confidence, raw_span in _frames_for_clause(clause):
                name = _clean_name(raw_span)
                if not name:
                    continue
                key = name.lower()
                if key not in best:
                    order.append(key)
                    best[key] = (confidence, name, clause)
                else:
                    # Keep the higher-confidence framing's evidence; order is
                    # already pinned by first appearance.
                    if confidence > best[key][0]:
                        best[key] = (confidence, name, clause)

    results: list[dict] = []
    for key in order:
        _conf, name, evidence = best[key]
        results.append({"name": name, "evidence": evidence})
        if len(results) >= MAX_SUGGESTIONS:
            break
    return results
