import { describe, expect, it } from 'vitest';
import { formatLifecycleDate, formatLifecycleTimestamp } from './lifecycleTimestamp';

describe('lifecycle timestamp formatting', () => {
  it('renders valid timestamps and safely suppresses malformed values', () => {
    expect(formatLifecycleTimestamp('2026-07-24T15:00:00.000Z')).toBeTruthy();
    expect(formatLifecycleTimestamp('not-a-timestamp')).toBeNull();
    expect(formatLifecycleDate('not-a-timestamp')).toBeNull();
  });
});
