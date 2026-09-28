/**
 * Skeleton primitives used when no cached data is available on first
 * paint. Matches the dark zinc palette and the fuchsia/sky accents the
 * rest of the app uses.
 *
 * Render shapes (cards, list rows, stat tiles) that mirror the real
 * UI as closely as possible so the only visible change when data lands
 * is the text/number filling in — no layout jump.
 */
import React from 'react';

interface SkeletonProps {
  className?: string;
  /** Width helpers — use Tailwind classes via className for full control. */
}

export const SkeletonBlock: React.FC<SkeletonProps> = ({ className }) => (
  <div
    className={`animate-pulse rounded-md bg-zinc-800/60 ${className || ''}`}
    aria-hidden="true"
  />
);

/**
 * Card-shaped skeleton matching the RoomCard layout. Used while the
 * /rooms list is fetching for the first time (no cache).
 */
export const SkeletonRoomCard: React.FC = () => (
  <div
    className="flex flex-col gap-3 rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5 shadow-lg shadow-black/30"
    aria-hidden="true"
  >
    <div className="flex items-start justify-between gap-2">
      <div className="min-w-0 flex-1 space-y-2">
        <SkeletonBlock className="h-4 w-2/3" />
        <SkeletonBlock className="h-3 w-1/3" />
      </div>
    </div>
    <SkeletonBlock className="h-3 w-1/2" />
    <SkeletonBlock className="h-3 w-3/4" />
    <div className="mt-1 flex items-center gap-2 pt-2 border-t border-zinc-800/80">
      <SkeletonBlock className="h-7 w-14" />
      <SkeletonBlock className="h-7 w-16" />
      <div className="ml-auto">
        <SkeletonBlock className="h-3 w-20" />
      </div>
    </div>
  </div>
);

/**
 * Grid of skeleton room cards matching the real /rooms grid layout
 * (1/2/3 columns at sm/lg breakpoints). Default count = 3, matching
 * lg breakpoint's row width.
 */
export const SkeletonRoomGrid: React.FC<{ count?: number }> = ({ count = 3 }) => (
  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
    {Array.from({ length: count }, (_, i) => (
      <SkeletonRoomCard key={i} />
    ))}
  </div>
);

/**
 * Row-shaped skeleton matching the RecentMeetings list item. Renders
 * inside the bordered container — render multiple to fill the panel.
 */
export const SkeletonMeetingRow: React.FC = () => (
  <div className="px-4 py-3" aria-hidden="true">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1 space-y-2">
        <SkeletonBlock className="h-4 w-3/4" />
        <SkeletonBlock className="h-3 w-full" />
        <div className="flex items-center gap-3 pt-1">
          <SkeletonBlock className="h-3 w-12" />
          <SkeletonBlock className="h-3 w-16" />
          <SkeletonBlock className="h-3 w-14" />
        </div>
      </div>
    </div>
  </div>
);

/**
 * Stat-tile skeleton matching the Dashboard StatsRow layout. Renders
 * 4 tiles in the same grid the real component uses.
 */
export const SkeletonStatsRow: React.FC = () => (
  <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
    {Array.from({ length: 4 }, (_, i) => (
      <div
        key={i}
        className="rounded-xl border border-zinc-800 bg-zinc-900/60 px-4 py-4 shadow-sm ring-1 ring-zinc-800/50"
        aria-hidden="true"
      >
        <div className="flex items-center justify-between">
          <SkeletonBlock className="h-3 w-16" />
          <SkeletonBlock className="h-4 w-4 rounded-full" />
        </div>
        <div className="mt-2">
          <SkeletonBlock className="h-7 w-20" />
        </div>
      </div>
    ))}
  </div>
);

/**
 * Skeleton for the Sessions grid view — uses a wider card to match
 * the real session-card padding.
 */
export const SkeletonSessionCard: React.FC = () => (
  <div
    className="bg-gray-900 border border-gray-800 rounded-lg p-6 space-y-4"
    aria-hidden="true"
  >
    <div className="flex items-start justify-between">
      <div className="flex-1 space-y-2">
        <SkeletonBlock className="h-5 w-3/4" />
        <SkeletonBlock className="h-3 w-1/2" />
      </div>
      <SkeletonBlock className="h-5 w-16 rounded-full" />
    </div>
    <SkeletonBlock className="h-3 w-full" />
    <SkeletonBlock className="h-3 w-5/6" />
    <div className="flex items-center gap-3 pt-2">
      <SkeletonBlock className="h-3 w-12" />
      <SkeletonBlock className="h-3 w-14" />
      <SkeletonBlock className="h-3 w-16" />
    </div>
  </div>
);

export const SkeletonSessionGrid: React.FC<{ count?: number }> = ({ count = 6 }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
    {Array.from({ length: count }, (_, i) => (
      <SkeletonSessionCard key={i} />
    ))}
  </div>
);
