"""Deployment feature flags.

These flags toggle whole subsystems on / off per deploy target. They are
read once at import time so they're cheap to check in hot endpoint paths.

Flags
-----
ROOM_MODE_ENABLED:
    Conference Room mode (Meeting-Ops appliance running on dedicated room
    hardware with a USB mic). On VPS / cloud deploys there is no physical
    mic and rooms cannot record, so we ship the endpoints disabled by
    default in cloud compose. Bigboy + on-prem appliance compose keep it
    on (default True). Endpoints return HTTP 503 when disabled.

PROJECTOPS_AUTO_PUSH_ACTION_ITEMS:
    Whether extracted action items are AUTOMATICALLY pushed to Project-Ops
    as tasks on meeting completion. Defaults OFF: not everything the model
    flags is a real task, so the auto-push is opt-in to avoid noise. This
    is only the *deployment default* for orgs that haven't set a per-org
    Project-Ops integration block — a per-org
    ``integrations.project_ops.auto_push_action_items`` flag overrides it
    (see ``services.integrations.org_config.resolve_project_ops``). Action
    item extraction, storage, and in-app display are unaffected either way;
    this gate only controls the automatic OUTBOUND push. Set this to true
    in compose only on deploys that want the legacy always-push behavior.

Add new flags here, then pass them through the per-deploy compose
``environment:`` block (see ``deploy/*/docker-compose.*.yml``).
"""
from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Conference Room mode — defaults ON (preserves bigboy appliance behavior).
# Cloud / VPS compose should set ROOM_MODE_ENABLED=false explicitly.
ROOM_MODE_ENABLED: bool = _bool("ROOM_MODE_ENABLED", True)

# Project-Ops action-item auto-push — defaults OFF. The automatic outbound
# push of extracted action items to Project-Ops is opt-in to keep
# model-flagged noise out of PO. This is only the fallback default for orgs
# without a per-org integration block; the per-org
# integrations.project_ops.auto_push_action_items flag wins when set.
PROJECTOPS_AUTO_PUSH_ACTION_ITEMS: bool = _bool(
    "PROJECTOPS_AUTO_PUSH_ACTION_ITEMS", False
)
