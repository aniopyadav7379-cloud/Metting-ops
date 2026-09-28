import React from 'react';
import type { AgentActionDiffEntry } from '../../types/agent-actions.types';

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return 'None';
  if (Array.isArray(value)) return value.join(', ') || '[]';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return '[object]';
    }
  }
  return String(value);
}

interface Props {
  diff: Record<string, AgentActionDiffEntry>;
}

export default function AgentActionDiff({ diff }: Props) {
  const entries = Object.entries(diff || {});
  if (entries.length === 0) return null;

  return (
    <div className="mt-3 rounded-lg border border-zinc-700 bg-zinc-900/70 p-3">
      <div className="mb-2 text-[11px] uppercase tracking-wider text-zinc-500">
        Changes
      </div>
      <div className="space-y-2">
        {entries.map(([key, entry]) => (
          <div key={key} className="rounded-md border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 text-xs font-medium text-zinc-200">{key}</div>
            <div className="grid gap-1 text-xs text-zinc-400 sm:grid-cols-2">
              <div>
                <span className="block text-[10px] uppercase tracking-wide text-zinc-600">From</span>
                <span className="block break-words">{formatValue(entry.from)}</span>
              </div>
              <div>
                <span className="block text-[10px] uppercase tracking-wide text-zinc-600">To</span>
                <span className="block break-words text-fuchsia-200">{formatValue(entry.to)}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

