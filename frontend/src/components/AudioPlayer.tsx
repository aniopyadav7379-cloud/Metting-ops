import React, { useEffect, useRef, useState } from 'react';
import { Play, Pause, Download, Volume2, RefreshCw } from 'lucide-react';

interface AudioPlayerProps {
  src: string;
  /** Filename hint for the download button. */
  downloadAs?: string;
  /** Subtitle / caption shown below the controls. */
  caption?: string;
  /** Optional click handler for a regenerate button. Hides when undefined. */
  onRegenerate?: () => void;
  regenerating?: boolean;
  className?: string;
  /** External ref forwarded to the underlying <audio> element so callers
   *  (e.g. SessionDetails) can seek via .currentTime when the user
   *  clicks a transcript segment or a timeline bar. */
  externalAudioRef?: React.MutableRefObject<HTMLAudioElement | null>;
  /** Called whenever playback position updates. Lets the parent
   *  highlight the active segment in the transcript. */
  onTimeUpdate?: (currentTime: number) => void;
}

function fmtTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return '0:00';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

/** Compact HTML5 audio player wired to look like the rest of the app.
 *  Streams the source URL with credentials so it works behind oauth2-proxy. */
export default function AudioPlayer({
  src,
  downloadAs,
  caption,
  onRegenerate,
  regenerating,
  className,
  externalAudioRef,
  onTimeUpdate,
}: AudioPlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // Mirror our local audio element to the caller's ref so external code
  // can seek/play without re-implementing the controls. Done in a ref
  // callback rather than passing externalAudioRef directly so both
  // refs end up wired without losing the local one we use for state.
  const setAudioEl = (el: HTMLAudioElement | null) => {
    audioRef.current = el;
    if (externalAudioRef) externalAudioRef.current = el;
  };
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(0.9);

  // Reset state when the source changes (e.g. regenerated audio).
  useEffect(() => {
    setPlaying(false);
    setPosition(0);
    setDuration(0);
  }, [src]);

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    a.volume = volume;
  }, [volume]);

  const onTogglePlay = () => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) {
      a.play().catch(() => undefined);
    } else {
      a.pause();
    }
  };

  const onSeek = (event: React.ChangeEvent<HTMLInputElement>) => {
    const a = audioRef.current;
    if (!a) return;
    const target = Number(event.target.value);
    a.currentTime = target;
    setPosition(target);
  };

  const onDownload = () => {
    const a = document.createElement('a');
    a.href = src;
    a.download = downloadAs || 'meeting-audio';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  return (
    <div className={`rounded-lg border border-zinc-200 bg-zinc-50 p-3 ${className || ''}`}>
      <audio
        ref={setAudioEl}
        src={src}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onLoadedMetadata={(e) => setDuration((e.target as HTMLAudioElement).duration || 0)}
        onTimeUpdate={(e) => {
          const t = (e.target as HTMLAudioElement).currentTime || 0;
          setPosition(t);
          if (onTimeUpdate) onTimeUpdate(t);
        }}
        onEnded={() => setPlaying(false)}
      />
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onTogglePlay}
          className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-purple-600 text-white shadow hover:bg-purple-500"
          aria-label={playing ? 'Pause' : 'Play'}
        >
          {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 translate-x-px" />}
        </button>
        <div className="flex-1">
          <input
            type="range"
            min={0}
            max={Math.max(duration, 0.001)}
            step={0.1}
            value={position}
            onChange={onSeek}
            className="w-full accent-purple-600"
          />
          <div className="mt-1 flex justify-between text-xs text-zinc-500">
            <span>{fmtTime(position)}</span>
            <span>{fmtTime(duration)}</span>
          </div>
        </div>
        <div className="hidden md:flex items-center gap-1 text-zinc-500">
          <Volume2 className="h-4 w-4" />
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={volume}
            onChange={(e) => setVolume(Number(e.target.value))}
            className="w-20 accent-purple-600"
          />
        </div>
        <button
          type="button"
          onClick={onDownload}
          className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-white text-zinc-600 shadow ring-1 ring-zinc-200 hover:text-purple-600"
          aria-label="Download"
        >
          <Download className="h-4 w-4" />
        </button>
        {onRegenerate && (
          <button
            type="button"
            onClick={onRegenerate}
            disabled={regenerating}
            className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-white text-zinc-600 shadow ring-1 ring-zinc-200 hover:text-purple-600 disabled:opacity-40"
            aria-label="Regenerate audio"
          >
            <RefreshCw className={`h-4 w-4 ${regenerating ? 'animate-spin' : ''}`} />
          </button>
        )}
      </div>
      {caption && <div className="mt-2 text-xs text-zinc-500">{caption}</div>}
    </div>
  );
}
