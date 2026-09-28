# Integrations — Meeting & Lecture Intelligence

Every integration follows the safe-default pattern: nothing fires
automatically when an action item, decision, or summary is extracted.
Pushing to an external system is always an explicit, user-confirmed API
call (`POST /api/action-items/{id}/push/{provider}` or `.../notify/{provider}`).

| Provider | Implemented | Contract Tested | Configured (this delivery) | Live Verified |
|---|---|---|---|---|
| Google Calendar | Yes | Yes | No (no OAuth app credentials) | **Blocked** — no network/credentials |
| Jira | Yes | Yes | No | **Blocked** |
| Todoist | Yes | Yes | No | **Blocked** |
| ClickUp | Yes | Yes | No | **Blocked** |
| Notion | Yes | Yes | No | **Blocked** |
| Confluence | Yes | Yes | No | **Blocked** |
| Slack | Yes | Yes | No | **Blocked** |
| Microsoft Teams | Yes | Yes | No | **Blocked** |
| Email | Yes | Yes | No | **Blocked** |

"Contract tested" means real request/response shapes against each
provider's documented API, verified with the HTTP transport layer mocked
(never the provider client's own logic) — see `tests/test_phase3g_integrations.py`
and `tests/test_google_calendar.py`. "Live verified" would mean a real
credential actually reached the provider's servers and got a real
response; that has not happened for any of the nine, and this document
does not claim otherwise.

## Setup per provider

### Google Calendar (OAuth)
1. Google Cloud Console → create an OAuth 2.0 Client (Web application).
2. Enable the Calendar API.
3. Register redirect URI: `https://<host>/api/integrations/google-calendar/callback`.
4. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
   `GOOGLE_OAUTH_REDIRECT_URI` in the environment.
5. In-app: Settings → Integrations → Google Calendar → Connect.

### Jira / Confluence (Atlassian Cloud, API token + Basic auth)
1. Atlassian account → Security → API tokens → create a token.
2. In-app: Settings → Integrations → Jira (or Confluence) → enable, set
   Base URL to your site (`https://yoursite.atlassian.net`), and API key
   to `your-email@company.com:the-api-token`.

### Todoist / ClickUp / Notion (personal/internal API token, Bearer or raw)
1. Generate a personal API token (Todoist: Settings → Integrations;
   ClickUp: Settings → Apps; Notion: create an internal integration at
   notion.so/my-integrations and share the target database with it).
2. In-app: Settings → Integrations → enable, set Base URL (defaults
   provided in the card description) and API key to the token.

### Slack / Microsoft Teams (Incoming Webhook)
1. Create an Incoming Webhook (Slack: api.slack.com/apps → Incoming
   Webhooks; Teams: channel → Connectors → Incoming Webhook).
2. In-app: Settings → Integrations → paste the webhook URL as the "API
   base URL" field — no separate API key needed for this auth mode.

### Email (SMTP)
1. In-app: Settings → Integrations → Email → enable, fill in SMTP host,
   port, username, from-address, and TLS toggle, and put the account
   password in the API key field.

## Why these auth modes, specifically

Notion and Microsoft Teams both support full OAuth apps, and Jira/
Confluence/ClickUp/Todoist could be OAuth too — this delivery
deliberately uses the simpler token/webhook modes (internal integration
token, personal API token, incoming webhook) instead, because they need
no app registration, no redirect URI, and no per-user consent flow to be
useful, and they're the auth modes each platform itself documents as the
standard path for this kind of server-to-server integration. Google
Calendar is the one exception requiring full OAuth, because Calendar
write access is fundamentally a per-user grant, not an org-level API key.
