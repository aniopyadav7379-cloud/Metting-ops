import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, ChevronUp, Circle, RefreshCw, Square } from 'lucide-react';
import { useAlwaysOn } from '../contexts/AlwaysOnContext';

function relTime(iso: string): string {
  const diffSec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min} min ago`;
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

type DocumentPictureInPictureWindow = Window & typeof globalThis;

interface DocumentPictureInPictureAPI {
  requestWindow: (options?: {
    width?: number;
    height?: number;
    disallowReturnToOpener?: boolean;
  }) => Promise<DocumentPictureInPictureWindow>;
  window?: DocumentPictureInPictureWindow | null;
}

interface AlwaysOnPopoutProps {
  requestKey: number;
}

export function isDocumentPictureInPictureSupported(): boolean {
  return typeof window !== 'undefined' && 'documentPictureInPicture' in window;
}

function formatDuration(seconds: number): string {
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  if (hrs > 0) {
    return `${hrs}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function copyStyles(target: Document): void {
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      const rules = Array.from(sheet.cssRules || [])
        .map((rule) => rule.cssText)
        .join('\n');
      if (rules) {
        const style = target.createElement('style');
        style.textContent = rules;
        target.head.appendChild(style);
      }
    } catch {
      const href = (sheet as CSSStyleSheet).href;
      if (!href) continue;
      const link = target.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      target.head.appendChild(link);
    }
  }
}

function PopoutContent() {
  const {
    state,
    vadActive,
    elapsedSeconds,
    summary,
    liveTranscript,
    stop,
    summarizing,
    modelLoadProgress,
  } = useAlwaysOn();
  const active = state === 'recording' || state === 'paused' || state === 'stopping';
  const lastLines = liveTranscript.slice(-3);
  const [historyOpen, setHistoryOpen] = useState(false);

  const latestSlice = summary.slices[summary.slices.length - 1];
  const older = useMemo(
    () => summary.slices.slice(0, -1).reverse(),
    [summary.slices],
  );

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-4">
      <div className="flex items-center justify-between gap-3 border-b border-zinc-800 pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Circle className={`h-3 w-3 ${active ? 'fill-red-500 text-red-500 animate-pulse' : 'text-zinc-600'}`} />
            Always-on
          </div>
          <div className="mt-1 text-xs text-zinc-500">{formatDuration(elapsedSeconds)}</div>
        </div>
        <div className={`h-2.5 w-2.5 rounded-full ${vadActive ? 'bg-emerald-400' : 'bg-zinc-700'}`} />
      </div>

      {modelLoadProgress !== null && (
        <div className="mt-3 h-1.5 overflow-hidden rounded bg-zinc-800">
          <div
            className="h-full bg-sky-400 transition-all"
            style={{ width: `${Math.round(modelLoadProgress * 100)}%` }}
          />
        </div>
      )}

      <div className="mt-4">
        <div className="flex items-center justify-between">
          <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">Latest summary</div>
          {latestSlice && (
            <div className="text-[10px] text-zinc-500">{relTime(latestSlice.createdAt)} · {latestSlice.modelLabel}</div>
          )}
        </div>

        {summary.currentDraft ? (
          <div className="mt-2 rounded-md border border-sky-500/40 bg-sky-500/5 p-2">
            <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wide text-sky-300">
              <RefreshCw className="h-3 w-3 animate-spin" />
              Generating
            </div>
            <div className="max-h-40 overflow-y-auto whitespace-pre-wrap text-sm leading-5 text-zinc-100">
              {summary.currentDraft}
            </div>
          </div>
        ) : (
          <div className="mt-2 max-h-44 overflow-y-auto whitespace-pre-wrap text-sm leading-6 text-zinc-200">
            {latestSlice?.text || (summarizing ? 'Preparing summary...' : 'No summary yet.')}
          </div>
        )}

        {older.length > 0 && (
          <div className="mt-3">
            <button
              type="button"
              onClick={() => setHistoryOpen((v) => !v)}
              className="flex items-center gap-1 text-[11px] font-medium text-zinc-400 hover:text-zinc-200"
            >
              {historyOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              {historyOpen ? 'Hide' : 'Show'} earlier slices ({older.length})
            </button>
            {historyOpen && (
              <div className="mt-2 max-h-40 space-y-2 overflow-y-auto pr-1">
                {older.map((slice) => (
                  <div key={slice.id} className="rounded border border-zinc-800 bg-black/30 p-2">
                    <div className="text-[10px] text-zinc-500">{relTime(slice.createdAt)} · {slice.modelLabel}</div>
                    <div className="mt-1 whitespace-pre-wrap text-xs leading-5 text-zinc-200">{slice.text}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="mt-4">
        <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">Transcript</div>
        <div className="mt-2 space-y-2">
          {lastLines.length > 0 ? lastLines.map((segment) => (
            <div key={segment.id} className="text-xs leading-5 text-zinc-300">
              {segment.speaker && (
                <span className="mr-1 font-medium text-zinc-100">{segment.speaker}:</span>
              )}
              {segment.text}
            </div>
          )) : (
            <div className="text-xs text-zinc-600">Waiting for speech.</div>
          )}
        </div>
      </div>

      <button
        onClick={stop}
        disabled={!active}
        className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-red-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Square className="h-4 w-4" />
        Stop
      </button>
    </div>
  );
}

export default function AlwaysOnPopout({ requestKey }: AlwaysOnPopoutProps) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const pipWindowRef = useRef<DocumentPictureInPictureWindow | null>(null);

  useEffect(() => {
    if (!requestKey || !isDocumentPictureInPictureSupported()) return;
    let cancelled = false;

    async function openPopout() {
      const api = (window as Window & {
        documentPictureInPicture?: DocumentPictureInPictureAPI;
      }).documentPictureInPicture;
      if (!api) return;

      const existing = pipWindowRef.current || api.window || null;
      if (existing && !existing.closed) {
        existing.focus();
        return;
      }

      const pipWindow = await api.requestWindow({
        width: 360,
        height: 480,
        disallowReturnToOpener: false,
      });
      if (cancelled) {
        pipWindow.close();
        return;
      }

      pipWindowRef.current = pipWindow;
      pipWindow.document.title = 'Meeting-Ops Always-on';
      copyStyles(pipWindow.document);
      const root = pipWindow.document.createElement('div');
      root.id = 'always-on-pip-root';
      pipWindow.document.body.innerHTML = '';
      pipWindow.document.body.style.margin = '0';
      pipWindow.document.body.appendChild(root);
      setContainer(root);

      pipWindow.addEventListener('pagehide', () => {
        pipWindowRef.current = null;
        setContainer(null);
      }, { once: true });
    }

    openPopout().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [requestKey]);

  return container ? createPortal(<PopoutContent />, container) : null;
}
