export interface NormalizedActionItem {
  text: string;
  owner?: string | null;
  dueDate?: string | null;
  status?: string | null;
  sessionId: string;
  sessionTitle?: string;
  sessionCreatedAt?: string;
}

interface ActionSource {
  id: string;
  title?: string;
  name?: string;
  created_at?: string;
  final_summary?: any;
  summary?: any;
  ai_insights?: any;
}

function coerceText(value: unknown): string {
  if (!value) return '';
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value === 'object') {
    const v: any = value;
    const candidate = v.action || v.text || v.title || v.description || v.task;
    if (typeof candidate === 'string') return candidate.trim();
  }
  return '';
}

function pickList(source: any): any[] {
  if (!source) return [];
  if (typeof source !== 'object') return [];
  const keys = ['action_items', 'actions', 'tasks', 'action-items'];
  for (const key of keys) {
    const v = source[key];
    if (Array.isArray(v) && v.length > 0) return v;
  }
  return [];
}

/**
 * The backend stores the AI-generated meeting summary across a few overlapping
 * JSON columns (final_summary, ai_insights, legacy summary). Each can carry
 * action items in two shapes: either a plain array of strings, or an array of
 * { action, owner, due_date, status } objects. Normalize both into a single
 * list keyed by session.
 */
export function extractActionItems(session: ActionSource): NormalizedActionItem[] {
  const buckets: any[] = [];
  if (session.final_summary && typeof session.final_summary === 'object') {
    buckets.push(session.final_summary);
  }
  if (session.ai_insights && typeof session.ai_insights === 'object') {
    buckets.push(session.ai_insights);
  }
  if (session.summary) {
    try {
      const parsed =
        typeof session.summary === 'string' ? JSON.parse(session.summary) : session.summary;
      if (parsed && typeof parsed === 'object') buckets.push(parsed);
    } catch {
      /* legacy non-JSON summary; ignore */
    }
  }

  const out: NormalizedActionItem[] = [];
  const seen = new Set<string>();
  const sessionTitle = session.title || session.name || 'Untitled meeting';

  for (const bucket of buckets) {
    const list = pickList(bucket);
    for (const raw of list) {
      const text = coerceText(raw);
      if (!text) continue;
      const key = `${session.id}:${text.slice(0, 120)}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const detail = typeof raw === 'object' && raw !== null ? raw : null;
      out.push({
        text,
        owner: detail ? detail.owner || detail.assignee || null : null,
        dueDate: detail ? detail.due_date || detail.dueDate || null : null,
        status: detail ? detail.status || null : null,
        sessionId: session.id,
        sessionTitle,
        sessionCreatedAt: session.created_at,
      });
    }
  }
  return out;
}
