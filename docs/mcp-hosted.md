# Meeting-Ops Hosted MCP Endpoint

Connect any AI client to your Meeting-Ops data over a single URL + token —
no clone, no install, no SDK upgrade. The same FastMCP server that runs
locally over stdio is exposed at `/mcp` on the Meeting-Ops backend over
[streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http).

## What is this?

Meeting-Ops ships a Model Context Protocol (MCP) server that gives an AI
client read-only access to your meetings: search, summaries, transcripts,
analytics, RAG, and AI insights. With the hosted endpoint, you point your
AI client at the public URL and supply a Personal Access Token (PAT). The
client makes the calls; the token tells the server which user (and which
organization, and which RBAC scope) the requests should run as.

Production endpoint:

```
https://meeting-ops.unicorncommander.ai/mcp
```

Self-hosted: substitute your own domain. The path is always `/mcp`.

## 1. Generate a Personal Access Token

1. Sign in to [Meeting-Ops Settings](https://meeting-ops.unicorncommander.ai/settings).
2. Open the **Personal Access Tokens** tab.
3. Click **New Token**, give it a name (e.g. `Claude Desktop`), and copy
   the `mops_pat_...` value that appears. You will not see it again.
4. Paste the token into your AI client's MCP config as `Authorization:
   Bearer <token>` (the snippets below take care of this for you).

PATs are SHA-256-hashed at rest. Anyone holding the plaintext can use it,
so treat it like a password. Revoke it from the same page whenever you
want.

## 2. Configure your AI client

Snippets below use `https://meeting-ops.unicorncommander.ai/mcp`. Replace
that with your own host if you self-host. Replace `mops_pat_XXX...` with
the token you minted.

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%/Claude/claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "meeting-ops": {
      "url": "https://meeting-ops.unicorncommander.ai/mcp",
      "headers": {
        "Authorization": "Bearer mops_pat_REPLACE_WITH_YOUR_TOKEN"
      }
    }
  }
}
```

Restart Claude Desktop. The 8 tools should appear in the slash-menu under
`meeting-ops`.

### Cursor

`.cursor/mcp.json` in your workspace (or `~/.cursor/mcp.json` to enable
it globally):

```json
{
  "mcpServers": {
    "meeting-ops": {
      "url": "https://meeting-ops.unicorncommander.ai/mcp",
      "headers": {
        "Authorization": "Bearer mops_pat_REPLACE_WITH_YOUR_TOKEN"
      }
    }
  }
}
```

### Continue

`~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: meeting-ops
    url: https://meeting-ops.unicorncommander.ai/mcp
    headers:
      Authorization: Bearer mops_pat_REPLACE_WITH_YOUR_TOKEN
```

### Zed

`~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "meeting-ops": {
      "url": "https://meeting-ops.unicorncommander.ai/mcp",
      "headers": {
        "Authorization": "Bearer mops_pat_REPLACE_WITH_YOUR_TOKEN"
      }
    }
  }
}
```

### Cline / Roo Code

Cline and Roo Code share the same MCP config in VS Code settings:

```json
{
  "mcp.servers": {
    "meeting-ops": {
      "url": "https://meeting-ops.unicorncommander.ai/mcp",
      "headers": {
        "Authorization": "Bearer mops_pat_REPLACE_WITH_YOUR_TOKEN"
      }
    }
  }
}
```

### ChatGPT Desktop / other clients

Any client that supports the
[MCP streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http)
will work. Set the server URL to `https://<your-host>/mcp` and add an
`Authorization: Bearer mops_pat_...` header.

## 3. What the AI can do

### Tools (read-only)

| Tool | What it does |
| --- | --- |
| `search_meetings(query, limit?)` | Hybrid semantic + keyword search across all your meetings. |
| `ask_about_meetings(question, limit?)` | RAG: ask a question, get an answer cited from your transcripts. |
| `list_meetings(status?, limit?, offset?)` | List recording sessions; filter by status. |
| `get_meeting_details(session_id)` | Title, status, duration, summary, decisions, action items. |
| `get_meeting_transcript(session_id, max_chars?)` | The transcript text, truncated if very long. |
| `chat_with_meeting(session_id, message)` | AI chat scoped to a single meeting's full context. |
| `get_analytics(time_range?)` | Session counts, speaker stats, recent trends. |
| `get_meeting_insights(session_id)` | AI keywords, topics, sentiment, action items. |

### Tools (propose → confirm)

Mutations are two-step: the AI proposes an action, you get a token, and
the AI calls `confirm_action(token)` after a human approves. Available:
`propose_create_session`, `propose_rename_session`, `propose_add_tag`,
`propose_remove_tag`, `propose_trigger_reprocess`, `propose_draft_followup_email`,
plus `confirm_action(token)` / `cancel_action(token)`.

This is intentional: hosted MCP starts read-only by default, mutations
require an explicit confirmation step. v3.13's agent-actions pattern.

### Resources

| URI | Returns |
| --- | --- |
| `meetings://list` | Overview of all meetings (latest 50). |
| `meetings://{session_id}` | Full details for one meeting. |

### Prompts

| Prompt | Purpose |
| --- | --- |
| `meeting_analysis(meeting_id)` | Deep-analysis workflow for one meeting. |
| `cross_meeting_research(topic)` | Multi-meeting research workflow on a topic. |

## 4. RBAC

Every tool call runs with the same RBAC scope as the user that minted
the PAT. Org-scoped data stays org-scoped. Free-tier limits still apply.
The MCP server is a thin client of the same backend REST API the SPA
talks to — there is no privileged bypass path.

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401 Unauthorized` with `Missing or malformed Authorization header` | Token isn't being sent | Re-check the `Authorization: Bearer mops_pat_...` header in your client config. |
| `401 Unauthorized` with `Invalid or revoked Personal Access Token` | Token was revoked or never existed | Mint a new one from Settings and update your client. |
| `403 Forbidden` (in a tool result) | Tier or RBAC gate on the underlying endpoint | The PAT is valid but the user can't access that data. Check the tier; check the org membership. |
| `404 Not Found` on `/mcp` | Hosted endpoint is disabled on the server | Confirm `MCP_HOSTED_ENABLED=true` and that the deploy includes the `/mcp` Traefik rule. |
| `503 Service Unavailable` | MCP session manager not started | Check backend logs; the lifespan startup may have raised. |
| Tools list is empty in the client UI | Client didn't complete the `initialize` handshake | Reload the client; check its MCP debug log. |

`GET https://<host>/health` returns an `mcp` field — `"ok"`, `"disabled"`,
or `"error: ..."` — for at-a-glance status.

## 6. Rate limits

The hosted endpoint inherits whatever rate limiting the underlying
backend enforces; there is no separate MCP-specific quota today.
External AI clients should respect ordinary HTTP 429 responses and back
off. If a single PAT starts hammering the endpoint, revoke it from the
Settings page and mint a new one.

## 7. Stdio mode (when you want local)

If you'd rather run the MCP server on your own machine (privacy,
offline, dev): clone the repo and follow
[`mcp/meeting_ops_mcp.py`](../mcp/meeting_ops_mcp.py)'s docstring. The
stdio entry uses the same FastMCP instance the hosted endpoint serves —
same tools, same behavior, same RBAC (it just talks to the backend over
loopback instead of HTTP).
