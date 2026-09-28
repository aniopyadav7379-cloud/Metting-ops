import React, { useMemo } from 'react';

interface TimelineSegment {
  start: number;
  end: number;
  speaker?: string | null;
}

interface Props {
  duration: number;          // total session length in seconds
  segments: TimelineSegment[];
  speakers: string[];         // display order top-to-bottom
  currentTime?: number;       // playback head position
  onSeek?: (time: number) => void;
}

// Stable palette — eight visually-distinct colors. Reused round-robin
// for sessions with more than 8 speakers (rare; pyannote rarely yields
// more once tuned at threshold 0.72).
const SPEAKER_COLORS = [
  '#8b5cf6', // purple
  '#06b6d4', // cyan
  '#10b981', // emerald
  '#f59e0b', // amber
  '#ef4444', // red
  '#ec4899', // pink
  '#3b82f6', // blue
  '#84cc16', // lime
];

function colorFor(speaker: string, index: number): string {
  return SPEAKER_COLORS[index % SPEAKER_COLORS.length];
}

function formatTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return '0:00';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/**
 * Speaker timeline / swimlane.
 *
 * One row per speaker, time on the x-axis. Each pyannote turn renders
 * as a colored block at the right horizontal offset. Click a block to
 * seek the audio player to that turn's start.
 *
 * Designed to live inside the transcript section header so users can
 * see who-talked-when at a glance without scrolling through the
 * transcript.
 */
export const SpeakerTimeline: React.FC<Props> = ({
  duration,
  segments,
  speakers,
  currentTime,
  onSeek,
}) => {
  const totalDuration = Math.max(duration, 0.001);

  // Group segments by speaker for rendering, ordered to match `speakers`.
  const grouped = useMemo(() => {
    const byKey: Record<string, TimelineSegment[]> = {};
    for (const seg of segments) {
      const key = seg.speaker || 'Speaker';
      if (!byKey[key]) byKey[key] = [];
      byKey[key].push(seg);
    }
    return speakers.map((sp, idx) => ({
      speaker: sp,
      color: colorFor(sp, idx),
      bars: byKey[sp] || [],
    }));
  }, [segments, speakers]);

  if (!speakers.length || totalDuration < 1) return null;

  return (
    <div className="rounded-md border border-gray-200 bg-gray-50 p-3 mb-3 flex-shrink-0">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Speaker timeline
        </span>
        <span className="text-xs text-gray-400">{formatTime(totalDuration)} total</span>
      </div>
      <div className="space-y-1.5">
        {grouped.map(({ speaker, color, bars }) => (
          <div key={speaker} className="flex items-center gap-2">
            <span
              className="text-[11px] font-medium truncate w-24 text-right"
              style={{ color }}
              title={speaker}
            >
              {speaker}
            </span>
            <div className="relative flex-1 h-4 bg-gray-200 rounded overflow-hidden">
              {bars.map((b, i) => {
                const left = (Math.max(0, b.start) / totalDuration) * 100;
                const width = Math.max(
                  0.2,  // 0.2% min so very short turns are still visible
                  ((Math.max(0, b.end - b.start)) / totalDuration) * 100,
                );
                return (
                  <button
                    key={i}
                    onClick={() => onSeek && onSeek(b.start)}
                    title={`${speaker}: ${formatTime(b.start)} → ${formatTime(b.end)}`}
                    className="absolute top-0 h-full hover:opacity-80 transition-opacity cursor-pointer"
                    style={{
                      left: `${left}%`,
                      width: `${width}%`,
                      backgroundColor: color,
                    }}
                  />
                );
              })}
              {/* Playhead marker */}
              {typeof currentTime === 'number' && currentTime > 0 && (
                <div
                  className="absolute top-0 h-full w-0.5 bg-black/70 pointer-events-none"
                  style={{ left: `${(currentTime / totalDuration) * 100}%` }}
                />
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default SpeakerTimeline;
