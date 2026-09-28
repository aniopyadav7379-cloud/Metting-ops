import React, { useMemo } from 'react';
import { Calendar, Clock, ListChecks, Users } from 'lucide-react';
import type { DashboardSession } from '../../pages/Dashboard';
import { extractActionItems } from './actionItems';

interface StatsRowProps {
  sessions: DashboardSession[];
  loading: boolean;
  detailsLoading: boolean;
}

function formatDuration(totalSeconds: number): string {
  if (!totalSeconds || totalSeconds <= 0) return '0m';
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

export const StatsRow: React.FC<StatsRowProps> = ({ sessions, loading, detailsLoading }) => {
  const stats = useMemo(() => {
    const now = Date.now();
    const weekAgo = now - 7 * 24 * 60 * 60 * 1000;

    let thisWeekCount = 0;
    let totalSeconds = 0;
    let pendingActions = 0;
    const speakerNames = new Set<string>();

    for (const session of sessions) {
      const created = new Date(session.created_at).getTime();
      if (Number.isFinite(created) && created >= weekAgo) {
        thisWeekCount += 1;
      }
      totalSeconds += session.duration || 0;
      const count = typeof session.speaker_count === 'number' ? session.speaker_count : 0;
      for (let i = 0; i < count; i += 1) {
        speakerNames.add(`${session.id}:${i}`);
      }
      for (const participant of session.participants || []) {
        if (participant?.name) {
          speakerNames.add(`name:${participant.name.toLowerCase()}`);
        }
      }
      const actions = extractActionItems(session as any);
      pendingActions += actions.length;
    }

    return {
      thisWeek: thisWeekCount,
      timeRecorded: formatDuration(totalSeconds),
      speakers: speakerNames.size,
      pending: pendingActions,
    };
  }, [sessions]);

  const cards = [
    {
      label: 'This week',
      sublabel: 'meetings',
      value: stats.thisWeek,
      icon: Calendar,
      tint: 'text-fuchsia-300',
      ring: 'ring-fuchsia-500/20',
    },
    {
      label: 'Time',
      sublabel: 'recorded',
      value: stats.timeRecorded,
      icon: Clock,
      tint: 'text-sky-300',
      ring: 'ring-sky-500/20',
    },
    {
      label: 'Speakers',
      sublabel: 'distinct',
      value: stats.speakers,
      icon: Users,
      tint: 'text-emerald-300',
      ring: 'ring-emerald-500/20',
    },
    {
      label: 'Actions',
      sublabel: 'pending',
      value: detailsLoading ? '…' : stats.pending,
      icon: ListChecks,
      tint: 'text-amber-300',
      ring: 'ring-amber-500/20',
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
      {cards.map((card) => {
        const Icon = card.icon;
        return (
          <div
            key={card.label}
            className={`rounded-xl border border-zinc-800 bg-zinc-900/60 px-4 py-4 shadow-sm ring-1 ${card.ring}`}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs uppercase tracking-wide text-zinc-500">{card.label}</span>
              <Icon className={`h-4 w-4 ${card.tint}`} />
            </div>
            <div className="mt-2 flex items-baseline gap-1">
              <span className="text-2xl font-semibold tabular-nums text-white sm:text-3xl">
                {loading ? '—' : card.value}
              </span>
              <span className="text-xs text-zinc-500">{card.sublabel}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
};
