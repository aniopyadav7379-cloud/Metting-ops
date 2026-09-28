"""Shared MCP app integrity tests.

Guards the invariant that the stdio entry point (``mcp/meeting_ops_mcp.py``)
and the hosted HTTP transport (``backend/api/mcp_http.py``) both expose
the SAME FastMCP instance — same tools, same resources, same prompts.
Drift between the two transports would silently break external clients.
"""

import asyncio
import importlib.util
import os
import sys


def test_mcp_app_module_imports():
    """backend.services.mcp_app imports cleanly and exposes the FastMCP instance."""
    from services.mcp_app import mcp, READONLY_TOOL_NAMES, set_pat, reset_pat
    assert mcp is not None
    assert mcp.name == "Meeting-Ops"
    # 8 original read-only tools + get_cross_app_hints (v3.22).
    assert len(READONLY_TOOL_NAMES) == 9
    # set_pat / reset_pat must round-trip without raising.
    tok = set_pat("mops_pat_unit_test")
    try:
        from services.mcp_app import _pat_var
        assert _pat_var.get() == "mops_pat_unit_test"
    finally:
        reset_pat(tok)


def test_mcp_readonly_tools_match_canonical_set():
    """Every canonical read-only tool must be registered on the shared instance."""
    from services.mcp_app import mcp, READONLY_TOOL_NAMES

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    for expected in READONLY_TOOL_NAMES:
        assert expected in names, f"missing read-only tool: {expected}"


def test_mcp_resources_registered():
    """meetings://list and meetings://{session_id} are registered as resources."""
    from services.mcp_app import mcp

    resources = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())
    uris = {str(r.uri) for r in resources} | {t.uriTemplate for t in templates}
    assert any("meetings://list" in u for u in uris)
    assert any("meetings://{session_id}" in u for u in uris)


def test_mcp_prompts_registered():
    """meeting_analysis and cross_meeting_research are registered as prompts."""
    from services.mcp_app import mcp

    prompts = asyncio.run(mcp.list_prompts())
    names = {p.name for p in prompts}
    assert "meeting_analysis" in names
    assert "cross_meeting_research" in names


def test_stdio_entry_point_imports_shared_instance():
    """The stdio entry script imports the same FastMCP instance as the
    backend services module, proving they will never silently drift."""
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    stdio_path = os.path.join(repo_root, "mcp", "meeting_ops_mcp.py")
    assert os.path.exists(stdio_path), f"stdio entry not found: {stdio_path}"

    # Use a unique module name so re-imports in the same pytest session
    # don't collide. Importing the script should NOT call mcp.run()
    # because it's guarded by `if __name__ == "__main__"`.
    spec = importlib.util.spec_from_file_location(
        "meeting_ops_mcp_stdio_under_test", stdio_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from services.mcp_app import mcp as canonical_mcp
    assert module.mcp is canonical_mcp


def test_context_vars_isolate_across_async_tasks():
    """Two concurrent async tasks must each see their own bound PAT."""
    from services.mcp_app import set_pat, reset_pat, _pat_var

    seen: list[tuple[str, str]] = []

    async def runner(label: str, pat: str):
        tok = set_pat(pat)
        try:
            await asyncio.sleep(0)  # force a context switch
            seen.append((label, _pat_var.get() or ""))
        finally:
            reset_pat(tok)

    async def main():
        await asyncio.gather(
            runner("a", "mops_pat_AAA"),
            runner("b", "mops_pat_BBB"),
        )

    asyncio.run(main())
    by_label = dict(seen)
    assert by_label["a"] == "mops_pat_AAA"
    assert by_label["b"] == "mops_pat_BBB"
