# Project-Ops action lifecycle staging evidence

This procedure is approval-gated because it creates and approves real staging
data. Do not run it against production.

## Preconditions

- Use a disposable staging workspace present in both apps with the same
  `workspace_id`.
- Allow `meeting-ops` in Project-Ops
  `PROJECT_OPS_FEDERATION_ACTORS`.
- The Brigade exchange must allow actor `meeting-ops`, audience `project-ops`,
  and scope `triage:write`.
- Meeting-Ops must have the Project-Ops integration enabled and
  `auto_push_action_items=true` for only the disposable workspace.
- Set Meeting-Ops `PROJECTOPS_PUBLIC_URL` to the same origin as Project-Ops
  `FRONTEND_URL` (HTTPS outside loopback development). This origin must serve
  `/dashboard/tasks/<task-id>` behind normal Project-Ops workspace auth.
  Bigboy defaults this pass-through to
  `https://projectops.magicunicorn.dev`; verify the rendered compose config
  before starting either the backend or reprocess worker:

  ```bash
  docker compose -f deploy/bigboy/docker-compose.bigboy.yml \
    --env-file deploy/bigboy/.env.bigboy config |
    grep PROJECTOPS_PUBLIC_URL
  ```
- Record `STAGING_ORGANIZATION_ID`, `STAGING_WORKSPACE_ID`, and a unique marker
  such as `MO-LIFECYCLE-YYYYMMDD-HHMM`.

## Evidence procedure

1. In Meeting-Ops staging, create one meeting and one action item containing
   only the unique marker. Leave the local checkbox open. Record the local
   action-item id.
2. Finalize/reprocess the meeting once. Verify the item reports `proposed`,
   has `project_ops_submitted_at`, and has no task id or URL.
3. Re-run finalize/reprocess. Verify Project-Ops still has exactly one proposal
   for `(workspaceId, MEETING_OPS, <action-item-id>)`.
4. Stop for a human Project-Ops reviewer. Approve the proposal into a disposable
   project, then press Approve again or replay the request. Verify exactly one
   task exists and the proposal keeps the same `createdTaskId`.
5. In the Meeting-Ops staging backend container, run:

   ```bash
   python scripts/reconcile_projectops_action_items.py \
     --organization-id "$STAGING_ORGANIZATION_ID" \
     --limit 10
   ```

6. Verify the item reports `approved_linked`, the canonical task id/URL,
   Project-Ops task status, and a last-successful-sync time. Open the link.
7. Mark the Meeting-Ops checkbox done. Verify the Project-Ops task status did
   not change.
8. Change the Project-Ops task status, use Refresh Project-Ops status in the
   Meeting-Ops detail, and verify only the read-only Project-Ops status changed;
   the Meeting-Ops checkbox did not.
9. With a token for a second staging workspace, submit/reconcile the first
   workspace's action-item id. Verify no proposal/task is returned or changed.
10. Temporarily break only the staging Project-Ops route, retry once, and verify
    `sync_failed`, a sanitized error code, attempt count, and retry control.
    Restore the route and retry; verify the existing proposal/task link returns
    without creating another task.

Capture request IDs from both services for steps 2, 5, and 10. Logs must contain
ids/counts only—never the bearer or action-item/meeting text.

## Cleanup

After evidence is accepted, a staging administrator should delete the
disposable Project-Ops task/project and the disposable Meeting-Ops meeting
(which cascades its action item), then remove the temporary integration opt-in.
If the staging environment retains triage proposals for audit, keep the
proposal row but delete the whole disposable workspace on the normal staging
workspace-retirement path. Record every deleted id and confirm that searches
for the unique marker return zero active tasks and meetings.
