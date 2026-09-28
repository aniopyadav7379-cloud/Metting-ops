const LIFECYCLE_FIELDS = [
  'project_ops_link_state',
  'project_ops_proposal_id',
  'project_ops_task_id',
  'project_ops_task_url',
  'project_ops_project_number',
  'project_ops_task_status',
  'project_ops_submitted_at',
  'project_ops_last_sync_attempt_at',
  'project_ops_last_synced_at',
  'project_ops_remote_updated_at',
  'project_ops_sync_error',
  'project_ops_retry_count',
  'project_ops_triage_submitted_at',
] as const;

type Identified = { id: number };

/**
 * Merge an automatic reconciliation response without touching Meeting-Ops
 * content or checkbox status. A concurrent local PATCH must never be undone by
 * a slower Project-Ops refresh response.
 */
export function mergeProjectOpsLifecycle<T extends Identified>(
  current: T[],
  refreshed: Array<Identified & Record<string, unknown>>,
): T[] {
  const byId = new Map(refreshed.map((item) => [item.id, item]));
  return current.map((item) => {
    const update = byId.get(item.id);
    if (!update) return item;
    const lifecyclePatch: Record<string, unknown> = {};
    for (const field of LIFECYCLE_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(update, field)) {
        lifecyclePatch[field] = update[field];
      }
    }
    return { ...item, ...lifecyclePatch };
  });
}

export function parseActionItemTarget(value: string | null): number | null {
  if (!value || !/^[1-9]\d*$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}
