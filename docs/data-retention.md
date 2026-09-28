# Meeting-Ops data retention

Canonical meeting audio is stored in Garage object storage; transcripts,
summaries, and metadata are stored in PostgreSQL, with derived search vectors
in Qdrant and meeting graph data in Brigade.

Automatic compliance deletion is off by default. Set
`MEETING_RETENTION_ENABLED=true` and `MEETING_RETENTION_DAYS` to a positive
number to establish the deployment default. An organization may override it
with `Organization.settings.retention_days`.

A conference room drives deletion of its sessions **only when it has explicitly
opted in** (`Room.retention_enabled = true`, added in migration 049, default
off). When a room has not opted in, its sessions fall back to the org/deployment
policy — a room never imposes an implicit retention horizon. (Earlier,
`Room.default_retention_days` carried a server default of 90, so every
room-attached session inherited a 90-day delete horizon even with no policy set;
the opt-in flag closes that data-loss footgun.) Sessions attached to a room with
`legal_hold` are never auto-deleted.

The daily Arq task uses the same hard-delete path as the product UI and removes
database rows, Garage media, Qdrant vectors, per-meeting chat history, and the
Brigade meeting subgraph. `0` means retain indefinitely.
