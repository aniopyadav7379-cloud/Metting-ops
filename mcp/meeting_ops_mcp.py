#!/usr/bin/env python3
"""
Meeting-Ops MCP Server — stdio entry point.

Exposes Meeting-Ops meeting intelligence as tools for external AI agents
via the Model Context Protocol (MCP). Connects to the Meeting-Ops backend
REST API (default: http://localhost:9050).

This file is intentionally thin. All tools, resources, and prompts live
in ``backend/services/mcp_app.py`` so the same FastMCP instance is shared
by the stdio entry (this file) and the hosted streamable-HTTP transport
(``backend/api/mcp_http.py``). The two transports MUST expose identical
capabilities — see READONLY_TOOL_NAMES in ``backend.services.mcp_app``.

Tools (read-only):
  - search_meetings, ask_about_meetings, list_meetings,
    get_meeting_details, get_meeting_transcript, chat_with_meeting,
    get_analytics, get_meeting_insights

Tools (propose → confirm; mutations require confirmation):
  - propose_create_session, propose_rename_session, propose_add_tag,
    propose_remove_tag, propose_trigger_reprocess,
    propose_draft_followup_email, confirm_action, cancel_action

Resources:
  - meetings://list, meetings://{session_id}

Prompts:
  - meeting_analysis, cross_meeting_research

Usage:
  # stdio transport (for Claude Desktop, etc.)
  MEETING_OPS_PAT=mops_pat_YOUR_TOKEN_HERE \\
  MEETING_OPS_URL=http://localhost:9050 \\
  python3 mcp/meeting_ops_mcp.py

Claude Desktop config (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "meeting-ops": {
        "command": "python3",
        "args": ["/path/to/UC-Meeting-Ops/mcp/meeting_ops_mcp.py"],
        "env": {
          "MEETING_OPS_URL": "http://localhost:9050",
          "MEETING_OPS_PAT": "mops_pat_YOUR_TOKEN_HERE"
        }
      }
    }
  }

For the hosted MCP endpoint (no clone, no install) see docs/mcp-hosted.md.
"""

import os
import sys

# Make backend/ importable when running this script directly out of the
# repo (the typical Claude Desktop config invocation). When the backend
# is installed as a package in production, this is a no-op.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", "backend"))
if os.path.isdir(_BACKEND) and _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Import the shared FastMCP instance. All tool / resource / prompt
# definitions live in backend.services.mcp_app.
from services.mcp_app import mcp, set_pat, set_backend_url  # noqa: E402


# ── Configuration ───────────────────────────────────────────────────────
BACKEND_URL = os.getenv("MEETING_OPS_URL", "http://localhost:9050")
PERSONAL_ACCESS_TOKEN = os.getenv("MEETING_OPS_PAT", "").strip()


if __name__ == "__main__":
    if not PERSONAL_ACCESS_TOKEN:
        sys.exit(
            "MEETING_OPS_PAT is required. Mint a token in Meeting-Ops "
            "Settings -> Personal Access Tokens, then set MEETING_OPS_PAT "
            "in your MCP client config."
        )

    # stdio runs in a single async context; setting these once for the
    # life of the process is enough — every tool call inherits them.
    set_pat(PERSONAL_ACCESS_TOKEN)
    set_backend_url(BACKEND_URL)

    mcp.run()
