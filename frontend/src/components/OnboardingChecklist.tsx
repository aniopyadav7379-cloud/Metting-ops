// First-run onboarding checklist for the Dashboard.
//
// Audit context (2026-05-29 Codex UX audit): new free-tier signups land on
// the Dashboard with `0 sessions` and no signposts. The dashboard's existing
// HeroActions/StatsRow tell them what they have, not what to do first.
// This component shows a row of dismissible cards aimed at the moves a
// first-run user needs to make.
//
// On devices that run browser inference (desktop-capable / desktop-fallback)
// the first card is the model-download prompt: Parakeet 0.6B INT8 (~890 MB)
// for live STT + a small LLM (~570 MB Qwen 3 0.6B or ~1.5-3 GB Gemma 4 E2B
// on WebGPU) for live summaries. ~1.4 GB on default Qwen settings, more on
// Gemma. Pre-2026-05-30 the user hit Record and the download started
// silently while they waited. The card lets them kick it off in advance,
// or at least know it's coming. On capture-only mobile we hide the card
// entirely because those users are on the server-completion path only.
//
// Visibility rule: only when the user has zero sessions, AND at least one
// card hasn't been dismissed. Dismissals persist per-card in localStorage
// so the user never has to dismiss the same card twice.

import { useMemo, useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Download, Mic, Radio, ShieldCheck, Sparkles, X } from 'lucide-react';
import { detectDevice, shouldRunBrowserInference } from '../utils/deviceDetection';
import { inBrowserSTT } from '../services/inBrowserSTT';
import { inBrowserLLM, getStoredModelId } from '../services/inBrowserLLM';

const STORAGE_KEY_PREFIX = 'meet:onboarding:dismissed:';

interface ChecklistItem {
  id: string;
  title: string;
  body: string;
  to: string;
  cta: string;
  icon: React.ComponentType<{ className?: string }>;
  iconBg: string;
  iconColor: string;
  /**
   * Optional click handler. When present, fires alongside the Link
   * navigation — used by the browser-models card to kick off STT + LLM
   * weight downloads in the background so the user doesn't sit through
   * them silently on their first Record click.
   */
  onClick?: () => void;
}

// Kicks off Parakeet 0.6B INT8 + the small browser LLM in the background.
// We deliberately swallow errors here: if WebGPU is unavailable, the
// network fails, or the user is on a forced-server path, the worst case is
// the card "did nothing" — the existing on-Record-click load path will
// still run and surface real errors there.
function preloadBrowserModels(): void {
  try {
    void inBrowserSTT.load();
  } catch {
    /* preload best-effort */
  }
  try {
    const modelId = getStoredModelId();
    if (modelId && modelId !== 'server') {
      void inBrowserLLM.preloadModel(modelId);
    }
  } catch {
    /* preload best-effort */
  }
}

const BROWSER_MODELS_ITEM: ChecklistItem = {
  id: 'browser-models',
  title: 'Download the AI models for live transcription',
  body:
    'Parakeet handles speech-to-text (~890 MB) and a small LLM writes live summaries (~570 MB Qwen, or larger Gemma 4 with WebGPU). About 1.4 GB total, one-time, cached forever.',
  to: '/record',
  cta: 'Start download now',
  icon: Download,
  iconBg: 'bg-sky-100',
  iconColor: 'text-sky-600',
  onClick: preloadBrowserModels,
};

const BASE_ITEMS: ChecklistItem[] = [
  {
    id: 'mic-test',
    title: 'Test your microphone',
    body: 'Pick the right input and confirm levels before your first recording.',
    to: '/settings#audio-devices',
    cta: 'Open audio settings',
    icon: Mic,
    iconBg: 'bg-purple-100',
    iconColor: 'text-purple-600',
  },
  {
    id: 'first-recording',
    title: 'Record a 60-second sample',
    body: 'Capture a quick personal recording to see live transcription in action.',
    to: '/record/personal',
    cta: 'Start recording',
    icon: Radio,
    iconBg: 'bg-fuchsia-100',
    iconColor: 'text-fuchsia-600',
  },
  {
    id: 'privacy-mode',
    title: 'Privacy mode keeps audio on your device',
    body: 'Local-only sessions never leave your browser. See where they live.',
    to: '/local-sessions',
    cta: 'View local sessions',
    icon: ShieldCheck,
    iconBg: 'bg-emerald-100',
    iconColor: 'text-emerald-600',
  },
  {
    id: 'pro-upgrade',
    title: 'Optional: upgrade to Pro for server completion',
    body: 'Server-side summaries, action items, and cross-meeting search.',
    to: '/pricing',
    cta: 'See plans',
    icon: Sparkles,
    iconBg: 'bg-amber-100',
    iconColor: 'text-amber-600',
  },
];

function buildItems(includeBrowserModels: boolean): ChecklistItem[] {
  // browser-models goes FIRST when the device can actually run them. On
  // capture-only mobile the LLM/STT cards never load locally so this card
  // would be a lie.
  return includeBrowserModels ? [BROWSER_MODELS_ITEM, ...BASE_ITEMS] : BASE_ITEMS;
}

function readDismissed(id: string): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY_PREFIX + id) === '1';
  } catch {
    return false;
  }
}

function writeDismissed(id: string) {
  try {
    window.localStorage.setItem(STORAGE_KEY_PREFIX + id, '1');
  } catch {
    /* ignore quota / disabled storage */
  }
}

interface Props {
  sessionCount: number;
}

export default function OnboardingChecklist({ sessionCount }: Props) {
  // Detect device once per mount. The capability class is stable for the
  // lifetime of the page, so a single read is enough.
  const items = useMemo(() => {
    const cap = detectDevice();
    return buildItems(shouldRunBrowserInference(cap));
  }, []);

  // Initialize from localStorage so the dismissals are persistent across
  // tab reloads. We can't put this in useMemo because we need to flip
  // state on user action.
  const [dismissed, setDismissed] = useState<Record<string, boolean>>(() => {
    const out: Record<string, boolean> = {};
    for (const item of items) out[item.id] = readDismissed(item.id);
    return out;
  });

  // Re-read on mount in case the dashboard hot-mounts and localStorage
  // changed in another tab.
  useEffect(() => {
    const next: Record<string, boolean> = {};
    let changed = false;
    for (const item of items) {
      const v = readDismissed(item.id);
      next[item.id] = v;
      if (v !== dismissed[item.id]) changed = true;
    }
    if (changed) setDismissed(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const visibleItems = useMemo(
    () => items.filter((item) => !dismissed[item.id]),
    [items, dismissed],
  );

  const dismiss = (id: string) => {
    writeDismissed(id);
    setDismissed((prev) => ({ ...prev, [id]: true }));
  };

  // Audit-defined visibility rule: show whenever the user has zero
  // sessions OR they still have at least one undismissed card. (The first
  // half catches the brand-new user; the second keeps the checklist
  // visible on subsequent loads until they actively dismiss each card.)
  const hasUndismissed = visibleItems.length > 0;
  if (sessionCount > 0 && !hasUndismissed) return null;
  if (!hasUndismissed) return null;

  const stepCount = items.length;

  return (
    <section
      aria-label="Getting started"
      className="mt-6 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4"
    >
      <header className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-zinc-100">
            Get set up in {stepCount} quick steps
          </h2>
          <p className="text-xs text-zinc-500">
            Dismiss any card you've already handled — they won't come back.
          </p>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {visibleItems.map((item) => {
          const Icon = item.icon;
          // For cards with an onClick side-effect (browser-models preload),
          // fire it on click AND dismiss the card; navigation still happens
          // via the Link href. Cards without onClick behave exactly as
          // before — Link nav only.
          const handleCtaClick = () => {
            if (item.onClick) {
              try {
                item.onClick();
              } catch {
                /* preload errors are non-fatal */
              }
              dismiss(item.id);
            }
          };
          return (
            <div
              key={item.id}
              className="relative flex flex-col gap-2 rounded-xl border border-zinc-800 bg-zinc-950 p-3 text-left"
            >
              <button
                type="button"
                onClick={() => dismiss(item.id)}
                aria-label={`Dismiss ${item.title}`}
                className="absolute right-2 top-2 rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
              >
                <X className="h-3.5 w-3.5" />
              </button>
              <div className={`inline-flex h-8 w-8 items-center justify-center rounded-lg ${item.iconBg}`}>
                <Icon className={`h-4 w-4 ${item.iconColor}`} />
              </div>
              <h3 className="text-sm font-semibold text-zinc-100 pr-6">{item.title}</h3>
              <p className="text-xs leading-5 text-zinc-400">{item.body}</p>
              <Link
                to={item.to}
                onClick={handleCtaClick}
                className="mt-auto inline-flex w-fit items-center gap-1 rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs font-medium text-zinc-200 hover:bg-zinc-800"
              >
                {item.cta}
              </Link>
            </div>
          );
        })}
      </div>
    </section>
  );
}
