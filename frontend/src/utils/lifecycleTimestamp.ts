/**
 * Render federation timestamps defensively. A malformed remote timestamp is
 * never useful enough to turn an action-item drawer into an "Invalid Date"
 * error state; the backend records the sanitized sync failure separately.
 */
export function formatLifecycleTimestamp(
  value: string | null | undefined,
): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toLocaleString();
}

export function formatLifecycleDate(
  value: string | null | undefined,
): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toLocaleDateString();
}
