import { describe, expect, it } from 'vitest';
import {
  mergeProjectOpsLifecycle,
  parseActionItemTarget,
} from './projectOpsLifecycle';

describe('Project-Ops lifecycle UI helpers', () => {
  it('merges linkage freshness without overwriting Meeting-Ops status/content', () => {
    const current = [
      {
        id: 41,
        text: 'Keep local content',
        status: 'done',
        project_ops_link_state: 'proposed',
        project_ops_task_status: null,
      },
    ];
    const merged = mergeProjectOpsLifecycle(current, [
      {
        id: 41,
        text: 'stale response content',
        status: 'todo',
        project_ops_link_state: 'approved_linked',
        project_ops_task_status: 'IN_PROGRESS',
        project_ops_task_url: 'https://projectops.example/dashboard/tasks/task-41',
      },
    ]);

    expect(merged[0]).toMatchObject({
      text: 'Keep local content',
      status: 'done',
      project_ops_link_state: 'approved_linked',
      project_ops_task_status: 'IN_PROGRESS',
    });
  });

  it('accepts only a positive safe integer deep-link target', () => {
    expect(parseActionItemTarget('41')).toBe(41);
    expect(parseActionItemTarget('0')).toBeNull();
    expect(parseActionItemTarget('41/other')).toBeNull();
    expect(parseActionItemTarget('1e2')).toBeNull();
    expect(parseActionItemTarget(null)).toBeNull();
  });
});
