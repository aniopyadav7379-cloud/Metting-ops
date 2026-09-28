"""Cross-app reference hints for Meeting-Ops MCP tool results.

Adds optional, best-effort handles to sibling Unicorn Commander apps
(Contact-Ops, Project-Ops, Crisis-Ops) on top of meeting payloads. The
user's AI client (Claude Desktop, Cursor, etc.) is the integration
layer — Meeting-Ops never calls those apps server-side. We just emit
small hints so the AI knows which sibling MCP/URL to query next.

Design principles:

* Never break if a sibling app is unreachable, misconfigured, or
  retired. Everything here is derived from data Meeting-Ops already
  owns (participants, title, summary text, project_app pointer).
* Schema first. The arrays may be empty — what matters is that the
  shape is stable and AI clients can rely on it.
* No NER, no external API calls, no LLM round-trips. Everything is
  string-level and deterministic so the hint payload stays cheap to
  attach to every tool response.
"""

from __future__ import annotations

import os
import re
from typing import Any

# ── Sibling app URLs ───────────────────────────────────────────────────
#
# Override via env in deployments where the canonical hostnames differ
# (on-prem appliances, dev rigs). The defaults match the documented
# production topology — see `reference_ecosystem_topology` in MEMORY.

CONTACT_OPS_URL = os.getenv(
    "CROSS_APP_CONTACT_OPS_URL", "https://contacts.magicunicorn.dev"
).rstrip("/")

PROJECT_OPS_URL = os.getenv(
    "CROSS_APP_PROJECT_OPS_URL", "https://project-ops.unicorncommander.ai"
).rstrip("/")

CRISIS_OPS_URL = os.getenv(
    "CROSS_APP_CRISIS_OPS_URL", "https://crisis.magicunicorn.dev"
).rstrip("/")


# ── Confidence model ───────────────────────────────────────────────────
#
# The numbers below are intentionally coarse. The AI client is the
# consumer, so they only need to be ordered enough to break ties when
# the AI decides which hint to chase first. We don't try to be a
# real entity-resolver here.
#
# 0.95 — exact match against a structured field (e.g. participant
#        record with email + name; project_app/project_slug pointer
#        already stored on the session).
# 0.70 — high-confidence string match (capitalized multi-word phrase
#        adjacent to a marker like "project" / "case").
# 0.40 — keyword-only heuristic match (single word, no surrounding
#        signal).

CONF_STRUCTURED = 0.95
CONF_STRONG_TEXT = 0.70
CONF_WEAK_TEXT = 0.40


# ── Public schema ──────────────────────────────────────────────────────


def empty_cross_app_references() -> dict[str, list[Any]]:
    """Return the canonical empty shape.

    Always returns the same keys so AI clients can rely on the schema
    even when Meeting-Ops can't derive any hints from the session.
    """
    return {
        "mentioned_contacts": [],
        "mentioned_projects": [],
        "mentioned_cases": [],
    }


# ── Hint builders (per sibling app) ─────────────────────────────────────


def _contact_ops_hint(*, name: str | None, email: str | None) -> dict[str, str]:
    """Build a Contact-Ops query hint.

    Prefers email (deterministic lookup); falls back to name.
    """
    if email:
        query = f"email:{email}"
    elif name:
        query = f"name:{name}"
    else:
        query = ""
    return {
        "app": "contact-ops",
        "url": CONTACT_OPS_URL,
        "query": query,
    }


def _project_ops_hint(*, name: str | None, slug: str | None = None) -> dict[str, str]:
    """Build a Project-Ops query hint.

    Project-Ops projects are UUID-keyed server-side; the AI client
    resolves by slug or name through the Project-Ops MCP.
    """
    if slug:
        query = f"slug:{slug}"
    elif name:
        query = f"name:{name}"
    else:
        query = ""
    return {
        "app": "project-ops",
        "url": PROJECT_OPS_URL,
        "query": query,
    }


def _crisis_ops_hint(*, name: str | None) -> dict[str, str]:
    """Build a Crisis-Ops query hint."""
    query = f"name:{name}" if name else ""
    return {
        "app": "crisis-ops",
        "url": CRISIS_OPS_URL,
        "query": query,
    }


# ── Text scanners (deliberately lightweight) ────────────────────────────

# "the X project" / "Project X" / "X project"
_PROJECT_PATTERN = re.compile(
    r"(?:\bthe\s+)?([A-Z][A-Za-z0-9][A-Za-z0-9 \-_/]{1,40}?)\s+project\b"
    r"|\bproject\s+([A-Z][A-Za-z0-9][A-Za-z0-9 \-_/]{1,40}?)\b",
)

# "X case" / "case X" / "the X matter"
_CASE_PATTERN = re.compile(
    r"\bcase\s+([A-Z][A-Za-z0-9][A-Za-z0-9 \-_/]{1,40}?)\b"
    r"|([A-Z][A-Za-z0-9][A-Za-z0-9 \-_/]{1,40}?)\s+(?:case|matter)\b",
)

# Skip noise tokens that are technically capitalized but not project names.
_PROJECT_STOPWORDS = {
    "this", "that", "the", "our", "their", "his", "her", "my", "your",
    "next", "last", "first", "second", "third", "main", "side", "new",
    "old", "current", "previous", "above", "below", "same", "other",
    "another", "every", "any", "all", "some", "no", "few", "many",
    "much", "more", "most", "less", "least", "such",
}


def _clean_candidate(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().strip(".,;:!?'\"")
    if not text:
        return None
    if text.lower() in _PROJECT_STOPWORDS:
        return None
    if len(text) < 2 or len(text) > 60:
        return None
    return text


def _scan_text_for_projects(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PROJECT_PATTERN.finditer(text or ""):
        for group in match.groups():
            name = _clean_candidate(group)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                found.append(name)
    return found


def _scan_text_for_cases(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _CASE_PATTERN.finditer(text or ""):
        for group in match.groups():
            name = _clean_candidate(group)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                found.append(name)
    return found


def _gather_text(session: dict[str, Any]) -> str:
    """Pull a small string blob from the session for keyword scanning.

    Intentionally avoids the full transcript — we don't want to pay an
    O(transcript_length) regex scan on every MCP call. Title + summary
    fields are enough for the lightweight heuristic.
    """
    chunks: list[str] = []
    for key in ("title", "name", "description"):
        v = session.get(key)
        if isinstance(v, str):
            chunks.append(v)

    final = session.get("final_summary")
    if isinstance(final, dict):
        for k in ("executive", "summary", "text"):
            v = final.get(k)
            if isinstance(v, str):
                chunks.append(v)
    elif isinstance(final, str):
        chunks.append(final)

    insights = session.get("ai_insights")
    if isinstance(insights, dict):
        for k in ("summary", "text"):
            v = insights.get(k)
            if isinstance(v, str):
                chunks.append(v)

    return "\n".join(chunks)


# ── Populator ──────────────────────────────────────────────────────────


def build_cross_app_references(session: dict[str, Any]) -> dict[str, list[Any]]:
    """Derive cross-app reference hints from a Meeting-Ops session payload.

    ``session`` is the JSON the backend's
    ``GET /api/simple/recording-sessions/{id}`` endpoint returns. We
    read a few well-known fields and emit best-effort hints. Anything
    that fails the cheap structural check is dropped silently — the
    caller never sees half-formed entries.
    """
    refs = empty_cross_app_references()

    if not isinstance(session, dict):
        return refs

    # --- mentioned_contacts: participants + diarized speakers ---------
    # Track both email and name keys so a structured participant with
    # email blocks a duplicate speaker-name-only entry from sneaking in.
    seen_contact_keys: set[str] = set()

    def _mark_seen(name: str | None, email: str | None) -> None:
        if email:
            seen_contact_keys.add(f"email:{email.lower()}")
        if name:
            seen_contact_keys.add(f"name:{name.lower()}")

    participants = session.get("participants")
    if isinstance(participants, list):
        for p in participants:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").strip() or None
            email = (p.get("email") or "").strip() or None
            if not name and not email:
                continue
            keys = {
                f"email:{email.lower()}" if email else None,
                f"name:{name.lower()}" if name else None,
            } - {None}
            if keys & seen_contact_keys:
                continue
            _mark_seen(name, email)
            refs["mentioned_contacts"].append({
                "name": name,
                "email": email,
                # Structured participant rows are the highest-confidence
                # signal we have — they were explicitly added to the
                # session, not inferred.
                "confidence": CONF_STRUCTURED,
                "contact_ops_hint": _contact_ops_hint(name=name, email=email),
            })

    # Fall back to the diarized-speaker list when the participants array
    # is empty (older sessions, sessions where the user didn't fill
    # attendees, etc.).
    diarized = session.get("transcript_diarized")
    if isinstance(diarized, dict):
        speakers = diarized.get("speakers")
        if isinstance(speakers, list):
            for sp in speakers:
                if isinstance(sp, dict):
                    name = (sp.get("name") or sp.get("label") or "").strip() or None
                elif isinstance(sp, str):
                    name = sp.strip() or None
                else:
                    name = None
                if not name:
                    continue
                # Skip generic "Speaker 1" / "SPK_00" placeholders — they
                # are not contactable people.
                if re.fullmatch(r"(?i)(speaker|spk|s)[ _\-]?\d+", name):
                    continue
                key = f"name:{name.lower()}"
                if key in seen_contact_keys:
                    continue
                _mark_seen(name, None)
                refs["mentioned_contacts"].append({
                    "name": name,
                    "email": None,
                    # Speaker labels without email are weaker — they
                    # might be a nickname or transcription artifact.
                    "confidence": CONF_STRONG_TEXT,
                    "contact_ops_hint": _contact_ops_hint(name=name, email=None),
                })

    # --- mentioned_projects: structured pointer + text scan -----------
    seen_project_keys: set[str] = set()

    project_app = session.get("project_app")
    project_slug = session.get("project_slug")
    project_id = session.get("project_id")
    if project_app == "project-ops" and (project_slug or project_id is not None):
        # The session is already wired to a Project-Ops project.
        name = project_slug or (str(project_id) if project_id is not None else None)
        if name:
            key = f"slug:{(project_slug or '').lower()}|id:{project_id}"
            seen_project_keys.add(key)
            refs["mentioned_projects"].append({
                "name": name,
                "confidence": CONF_STRUCTURED,
                "project_ops_hint": _project_ops_hint(
                    name=name, slug=project_slug
                ),
            })

    blob = _gather_text(session)
    for candidate in _scan_text_for_projects(blob):
        key = f"name:{candidate.lower()}"
        if key in seen_project_keys:
            continue
        seen_project_keys.add(key)
        refs["mentioned_projects"].append({
            "name": candidate,
            "confidence": CONF_WEAK_TEXT,
            "project_ops_hint": _project_ops_hint(name=candidate),
        })

    # --- mentioned_cases: text scan only ------------------------------
    seen_case_keys: set[str] = set()
    for candidate in _scan_text_for_cases(blob):
        key = f"name:{candidate.lower()}"
        if key in seen_case_keys:
            continue
        seen_case_keys.add(key)
        refs["mentioned_cases"].append({
            "name": candidate,
            "confidence": CONF_WEAK_TEXT,
            "crisis_ops_hint": _crisis_ops_hint(name=candidate),
        })

    return refs


# ── Markdown rendering helper ───────────────────────────────────────────


def render_cross_app_section(refs: dict[str, list[Any]]) -> str:
    """Render the cross-app references as a compact markdown section.

    Returns the empty string when there's nothing to show, so callers
    can blindly append the result to their formatted string output.

    Format: a single ``## Cross-App References`` heading followed by
    one bullet per hint and a fenced JSON block carrying the full
    structured payload. The fenced block is what AI clients should
    parse — the bullets are for human-readable rendering in chat UIs.
    """
    import json as _json

    has_any = any(refs.get(k) for k in ("mentioned_contacts", "mentioned_projects", "mentioned_cases"))
    if not has_any:
        return ""

    lines = ["", "## Cross-App References"]
    lines.append(
        "_Sibling Unicorn Commander apps your AI client can call to resolve these handles._"
    )

    contacts = refs.get("mentioned_contacts", []) or []
    if contacts:
        lines.append("\n**Contacts (Contact-Ops):**")
        for c in contacts:
            label = c.get("name") or c.get("email") or "unknown"
            email = c.get("email")
            conf = c.get("confidence", 0)
            extra = f" <{email}>" if email else ""
            lines.append(f"- {label}{extra} (confidence: {conf:.2f})")

    projects = refs.get("mentioned_projects", []) or []
    if projects:
        lines.append("\n**Projects (Project-Ops):**")
        for p in projects:
            lines.append(
                f"- {p.get('name', 'unknown')} (confidence: {p.get('confidence', 0):.2f})"
            )

    cases = refs.get("mentioned_cases", []) or []
    if cases:
        lines.append("\n**Cases (Crisis-Ops):**")
        for c in cases:
            lines.append(
                f"- {c.get('name', 'unknown')} (confidence: {c.get('confidence', 0):.2f})"
            )

    lines.append("\n```json")
    lines.append(_json.dumps({"cross_app_references": refs}, indent=2, default=str))
    lines.append("```")
    return "\n".join(lines)


__all__ = [
    "CONTACT_OPS_URL",
    "PROJECT_OPS_URL",
    "CRISIS_OPS_URL",
    "CONF_STRUCTURED",
    "CONF_STRONG_TEXT",
    "CONF_WEAK_TEXT",
    "empty_cross_app_references",
    "build_cross_app_references",
    "render_cross_app_section",
]
