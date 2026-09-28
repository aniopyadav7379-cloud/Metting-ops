/**
 * Public marketing / product page for Meeting-Ops, rendered at `/` when
 * `VITE_LANDING_PAGE_ENABLED === "true"` AND the visitor is not
 * authenticated. Bigboy dev keeps the historical `/` → `/dashboard`
 * redirect via `VITE_LANDING_PAGE_ENABLED=false` (compose default).
 *
 * This is a long-scroll, best-in-class product page: nav → hero →
 * trust strip → how-it-works → the privacy/moat section → feature rows
 * (all 6 real product screenshots, framed) → pricing teaser → FAQ →
 * final CTA + invite funnel → footer. Brand = navy + gold "comms
 * officer" (tailwind.config.js remaps fuchsia→gold / indigo→navy; this
 * file uses the explicit `navy-*` / `gold-*` tokens).
 *
 * Intentionally:
 * - NO API calls on mount (no /me probe). The only network call is the
 *   user-initiated, fire-and-forget invite-request POST in handleSubmit.
 * - NO external JS — no third-party fonts (self-hosted), no GA/Segment.
 *   The only JS is a small IntersectionObserver (scroll-reveal), a
 *   scroll listener (sticky-nav state), and the mobile menu toggle.
 * - In-page navigation uses scrollIntoView (NOT `#anchor` hrefs) so it
 *   never mutates the hash and collides with the HashRouter. Real routes
 *   (Sign in → `#/login`) keep the `#/` hash form.
 *
 * The invite funnel (email → fire-and-forget log → redirect to
 * `/signup?email=`) is unchanged and covered by Landing.test.tsx — it is
 * robust to the live invite-only beta (Signup pivots gracefully on a 403)
 * and to a future open self-serve flip. CTA copy can flip from
 * "Request early access" to "Start free" when registration opens — see
 * the `BETA` flag below.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Apple,
  ArrowRight,
  BookOpen,
  Building2,
  CalendarDays,
  Check,
  FileDown,
  Fingerprint,
  Github,
  Globe,
  Lock,
  Maximize2,
  Menu,
  MessageSquare,
  Mic,
  Plug,
  Plus,
  Radio,
  Server,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Upload,
  Users,
  X,
} from 'lucide-react';
import { config } from '../config';
import LegalFooter from '../components/LegalFooter';
import { track } from '../utils/posthog';
import { beginProCheckout, hasStoredAuthToken } from '../utils/checkout';
import {
  PRO_MONTHLY_PRICE_DISPLAY,
  PRO_MONTHLY_PRICE_WITH_PERIOD,
  PRO_LIST_PRICE_DISPLAY,
  PRO_LAUNCH_DISCOUNT_PCT,
} from '../constants/pricing';

type SubmitState = 'idle' | 'submitting' | 'success' | 'error';

/**
 * Environment-aware positioning. The PRODUCTION host
 * (`*.unicorncommander.ai`) presents as a live, open product — "Start
 * free". Every other host (`*.magicunicorn.dev` dogfood, localhost) is
 * the PRIVATE BETA — "Request early access". The invite funnel
 * (email → /signup) is identical either way; only the framing differs,
 * and Signup pivots gracefully if self-serve registration is still gated.
 *
 * NOTE: for "Start free" to be true one-click self-serve on production, the
 * backend gate `REQUIRE_INVITE_CODE` must be `false` on that node; until
 * then the Signup page surfaces the invite step. Toggle is host-derived so
 * the same bundle is correct on both nodes.
 */
const isProductionHost =
  typeof window !== 'undefined' &&
  /(?:^|\.)unicorncommander\.ai$/i.test(window.location.hostname);
const BETA = !isProductionHost;

// ---------------------------------------------------------------------------
// Small reusable primitives
// ---------------------------------------------------------------------------

const btnPrimary =
  'inline-flex items-center justify-center gap-2 rounded-xl bg-gold-500 px-5 py-3 text-sm font-semibold text-navy-950 shadow-[0_0_24px_-6px_rgba(212,175,55,0.5)] ring-1 ring-gold-400/50 transition hover:bg-gold-400 hover:shadow-[0_0_30px_-4px_rgba(212,175,55,0.6)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-300 focus-visible:ring-offset-2 focus-visible:ring-offset-navy-950 active:translate-y-px motion-reduce:transition-none';

const btnSecondary =
  'inline-flex items-center justify-center gap-2 rounded-xl bg-white/5 px-5 py-3 text-sm font-medium text-zinc-100 ring-1 ring-white/15 transition hover:bg-white/10 hover:ring-white/25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-navy-950 motion-reduce:transition-none';

const ghostLink =
  'group inline-flex items-center gap-1.5 text-sm font-medium text-zinc-300 transition hover:text-white focus-visible:outline-none focus-visible:text-white';

/** A static-skyline / breathing audio waveform — the page's "heartbeat". */
const WAVE = [
  34, 58, 42, 72, 48, 86, 52, 66, 38, 62, 80, 46, 56, 40, 76, 50, 64, 32, 54,
  70, 44, 60, 50, 82,
];
const Waveform: React.FC<{ className?: string }> = ({ className = 'h-4' }) => (
  <span
    aria-hidden="true"
    className={`wave inline-flex items-end gap-[2px] ${className}`}
  >
    {WAVE.map((h, i) => (
      <span
        key={i}
        className="w-[2px] rounded-full bg-gold-400/80"
        style={{ height: `${h}%`, animationDelay: `${i * 0.06}s` }}
      />
    ))}
  </span>
);

/** Soft radial "aurora" bloom — navy core with a faint gold filament. */
const Aurora: React.FC<{ className?: string; gold?: boolean }> = ({
  className = '',
  gold = false,
}) => (
  <div aria-hidden="true" className={`pointer-events-none absolute -z-10 ${className}`}>
    <div className="absolute inset-0 rounded-full bg-[radial-gradient(closest-side,rgba(40,79,140,0.55),rgba(20,45,84,0.22),transparent)] blur-[120px] opacity-70" />
    {gold && (
      <div className="absolute left-1/2 top-1/4 h-32 w-2/3 -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(212,175,55,0.30),transparent)] blur-[80px] opacity-50" />
    )}
  </div>
);

/** Scroll-reveal wrapper (reduced-motion safe via the `.reveal` CSS rule). */
/**
 * `?static=1` renders every Reveal pre-shown (no scroll animation).
 * Screenshot/capture tooling uses it — headless full-page captures have
 * no real scrolling, so IntersectionObserver may never fire and the page
 * below the fold captures as a void. Real visitors never hit this.
 */
const STATIC_CAPTURE = (() => {
  try {
    return new URLSearchParams(window.location.search).has('static');
  } catch {
    return false;
  }
})();

const Reveal: React.FC<{
  children: React.ReactNode;
  className?: string;
  delay?: number;
}> = ({ children, className = '', delay = 0 }) => {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(STATIC_CAPTURE);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === 'undefined') {
      setShown(true);
      return;
    }
    const ob = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setShown(true);
          ob.disconnect();
        }
      },
      { rootMargin: '0px 0px -10% 0px', threshold: 0.12 },
    );
    ob.observe(el);
    return () => ob.disconnect();
  }, []);
  return (
    <div
      ref={ref}
      className={`reveal ${shown ? 'is-revealed' : ''} ${className}`}
      style={{ transitionDelay: shown ? `${delay}ms` : '0ms' }}
    >
      {children}
    </div>
  );
};

/** Eyebrow + headline (+ optional subhead) — used by every section. */
const SectionHeader: React.FC<{
  eyebrow: string;
  title: React.ReactNode;
  sub?: React.ReactNode;
  align?: 'center' | 'left';
  id?: string;
}> = ({ eyebrow, title, sub, align = 'center', id }) => (
  <div className={align === 'center' ? 'mx-auto max-w-2xl text-center' : 'max-w-xl'}>
    <p className="font-mono text-xs uppercase tracking-[0.2em] text-gold-400">
      {eyebrow}
    </p>
    <h2
      id={id}
      className="mt-4 font-display text-3xl font-semibold tracking-[-0.02em] text-white sm:text-4xl lg:text-5xl"
    >
      {title}
    </h2>
    {sub && <p className="mt-5 text-lg leading-relaxed text-zinc-400">{sub}</p>}
  </div>
);

/** Premium framed product screenshot with faux-browser chrome + glow.
 *  Click anywhere on it to open the full-size view (onEnlarge). */
const FramedShot: React.FC<{
  src: string;
  alt: string;
  slug: string;
  gold?: boolean;
  priority?: boolean;
  className?: string;
  onEnlarge?: (src: string, alt: string) => void;
}> = ({ src, alt, slug, gold = false, priority = false, className = '', onEnlarge }) => (
  <div className={`group relative isolate ${className}`}>
    <div
      aria-hidden="true"
      className={`pointer-events-none absolute -inset-x-8 -top-10 bottom-0 -z-10 opacity-60 blur-[80px] transition-opacity duration-500 group-hover:opacity-90 ${
        gold
          ? 'bg-[radial-gradient(closest-side,rgba(212,175,55,0.20),transparent)]'
          : 'bg-[radial-gradient(closest-side,rgba(40,79,140,0.45),rgba(20,45,84,0.2),transparent)]'
      }`}
    />
    <button
      type="button"
      onClick={() => onEnlarge?.(`/screenshots/${src}.png`, alt)}
      aria-label={`Enlarge screenshot: ${alt}`}
      className="relative block w-full cursor-zoom-in overflow-hidden rounded-2xl bg-navy-900/80 text-left shadow-2xl shadow-black/60 ring-1 ring-white/10 transition hover:ring-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/60 motion-reduce:transition-none"
    >
      <div className="flex h-9 items-center gap-2 border-b border-white/10 bg-white/[0.03] px-4">
        <span className="h-2.5 w-2.5 rounded-full bg-white/15" />
        <span className="h-2.5 w-2.5 rounded-full bg-white/15" />
        <span className="h-2.5 w-2.5 rounded-full bg-gold-500/60" />
        <span className="ml-3 hidden min-w-0 flex-1 sm:flex">
          <span className="inline-flex max-w-[16rem] items-center truncate rounded-md bg-white/[0.04] px-2 py-0.5 font-mono text-[11px] text-zinc-500 ring-1 ring-white/10">
            meeting-ops.app{slug}
          </span>
        </span>
        <span
          aria-hidden="true"
          className="ml-auto hidden items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500 transition group-hover:text-gold-300 sm:inline-flex"
        >
          <Maximize2 className="h-3 w-3" />
          enlarge
        </span>
      </div>
      <img
        src={`/screenshots/${src}.png`}
        alt={alt}
        width={1986}
        height={953}
        loading={priority ? 'eager' : 'lazy'}
        decoding="async"
        className="block w-full transition-transform duration-500 group-hover:scale-[1.015] motion-reduce:transform-none"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-9 h-16 bg-gradient-to-b from-white/[0.06] to-transparent"
      />
    </button>
  </div>
);

// ---------------------------------------------------------------------------
// Sticky nav (scroll-state + mobile menu)
// ---------------------------------------------------------------------------

const NAV_LINKS: Array<[string, string]> = [
  ['How it works', 'how'],
  ['Privacy', 'privacy'],
  ['Speakers', 'speakers'],
  ['Features', 'features'],
  ['Pricing', 'pricing'],
  ['FAQ', 'faq'],
];

const Nav: React.FC<{ onAnchor: (id: string) => void }> = ({ onAnchor }) => {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const go = (id: string) => {
    setOpen(false);
    onAnchor(id);
  };

  return (
    <header
      data-scrolled={scrolled}
      className="fixed inset-x-0 top-0 z-50 transition-colors duration-300 data-[scrolled=true]:border-b data-[scrolled=true]:border-white/10 data-[scrolled=true]:bg-navy-950/70 data-[scrolled=true]:backdrop-blur-xl"
    >
      <nav
        aria-label="Main"
        className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6 lg:px-8"
      >
        <a
          href="#/"
          onClick={(e) => {
            e.preventDefault();
            window.scrollTo({ top: 0, behavior: 'smooth' });
          }}
          className="flex items-center gap-2.5 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/50"
        >
          <span className="flex h-8 w-8 items-center justify-center overflow-hidden rounded-lg bg-navy-900 ring-1 ring-gold-500/30">
            <img
              src="/brand/meeting-ops-mark.png?v=3"
              alt="Meeting-Ops"
              className="h-full w-full object-cover"
            />
          </span>
          <span className="font-display text-lg font-semibold tracking-tight text-white">
            Meeting-Ops
          </span>
        </a>

        <div className="hidden items-center gap-8 lg:flex">
          {NAV_LINKS.map(([label, id]) => (
            <button
              key={id}
              type="button"
              onClick={() => go(id)}
              className="text-sm text-zinc-400 transition-colors hover:text-white focus-visible:text-white focus-visible:outline-none"
            >
              {label}
            </button>
          ))}
        </div>

        <div className="hidden items-center gap-3 lg:flex">
          <a
            href="#/login"
            className="text-sm text-zinc-300 transition-colors hover:text-white"
          >
            Sign in
          </a>
          <button type="button" onClick={() => go('invite')} className={btnSecondary}>
            {BETA ? 'Request access' : 'Start free'}
          </button>
        </div>

        <button
          type="button"
          aria-label={open ? 'Close menu' : 'Open menu'}
          aria-expanded={open}
          aria-controls="mobile-menu"
          onClick={() => setOpen((v) => !v)}
          className="flex h-11 w-11 items-center justify-center rounded-lg text-zinc-300 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/50 lg:hidden"
        >
          {open ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
        </button>
      </nav>

      {open && (
        <div
          id="mobile-menu"
          className="border-t border-white/10 bg-navy-950/95 px-6 py-6 backdrop-blur-xl lg:hidden"
        >
          <div className="flex flex-col gap-1">
            {NAV_LINKS.map(([label, id]) => (
              <button
                key={id}
                type="button"
                onClick={() => go(id)}
                className="flex min-h-[44px] items-center py-3 text-left text-base text-zinc-200 hover:text-white"
              >
                {label}
              </button>
            ))}
            <a
              href="#/login"
              onClick={() => setOpen(false)}
              className="flex min-h-[44px] items-center py-3 text-base text-zinc-200 hover:text-white"
            >
              Sign in
            </a>
            <button
              type="button"
              onClick={() => go('invite')}
              className={`${btnPrimary} mt-3 w-full`}
            >
              {BETA ? 'Request early access' : 'Start free'}
            </button>
          </div>
        </div>
      )}
    </header>
  );
};

// ---------------------------------------------------------------------------
// Static content
// ---------------------------------------------------------------------------

// Hero headlines — one is drawn at random per page load (Aaron: rotate on
// refresh, never live-cycle). The gold-gradient span is the `wink` segment.
// SEO/social stay anchored: <title>, OG tags, JSON-LD, and the noscript block
// all carry the canonical "memory, decisions, and work" line regardless of
// which headline a given visit draws.
const HERO_HEADLINES: Array<{ pre: string; wink: string; post: string }> = [
  { pre: 'Your meetings become ', wink: 'memory, decisions, and work.', post: '' },
  { pre: 'The only attendee with a ', wink: 'perfect memory.', post: '' },
  { pre: 'Never ask ', wink: '“wait, who said that?”', post: ' again.' },
  { pre: 'Meetings end. ', wink: 'Decisions shouldn’t.', post: '' },
  { pre: 'Minutes, ', wink: 'without anyone taking them.', post: '' },
  { pre: 'Talk is cheap. ', wink: 'Your transcript is free.', post: '' },
  { pre: 'You run the meeting. ', wink: 'It runs the follow-up.', post: '' },
  { pre: '“Didn’t we decide this already?” ', wink: 'It has receipts.', post: '' },
  { pre: '“As discussed.” ', wink: 'Now with proof.', post: '' },
  { pre: 'Remembers every word. ', wink: 'Judges no one.', post: '' },
  { pre: 'Speak. ', wink: 'It does the paperwork.', post: '' },
  { pre: 'Your meetings, ', wink: 'with a search bar.', post: '' },
];

// Module scope = evaluated exactly once per page load.
const HERO_HEADLINE =
  HERO_HEADLINES[Math.floor(Math.random() * HERO_HEADLINES.length)];

const TRUST_CHIPS: Array<{ icon: React.ReactNode; label: string }> = [
  { icon: <ShieldCheck className="h-4 w-4 text-gold-400/80" />, label: 'Live transcription on-device' },
  { icon: <Lock className="h-4 w-4 text-gold-400/80" />, label: 'Privacy Mode: 0 uploads' },
  { icon: <Server className="h-4 w-4 text-gold-400/80" />, label: 'We don’t train on your data' },
  { icon: <Building2 className="h-4 w-4 text-gold-400/80" />, label: 'Open source · self-host' },
];

const STEPS: Array<{ n: string; icon: React.ReactNode; title: string; body: string }> = [
  {
    n: '01',
    icon: <Mic className="h-5 w-5" />,
    title: 'Hit record',
    body: 'Open Meeting-Ops in your browser and start any meeting or call — no bot to invite, no app to install.',
  },
  {
    n: '02',
    icon: <ShieldCheck className="h-5 w-5" />,
    title: 'Watch it transcribe',
    body: 'A live transcript and a rolling summary appear as you talk, generated on-device, in real time. In Privacy Mode, nothing leaves your machine.',
  },
  {
    n: '03',
    icon: <Sparkles className="h-5 w-5" />,
    title: 'Get the record',
    body: 'When you stop, an optional server pass returns a polished, speaker-by-speaker transcript with action items, decisions, and full-library search.',
  },
];

const THEM = [
  'Audio streamed to third-party APIs (AssemblyAI, Deepgram)',
  'Your words sent to OpenAI or Anthropic to summarize',
  'Your meetings may train someone else’s model',
  'Their cloud, their terms — take it or leave it',
];
const US = [
  'Live transcription runs in your own browser',
  'By default, the server pass runs on hardware we operate',
  'We never train on your data. Full stop.',
  'Cloud, on-prem, or fully self-hosted — your call',
];

const STATS: Array<{ value: string; unit?: string; label: string; gold?: boolean }> = [
  { value: '$0', label: 'Marginal compute per live hour', gold: true },
  { value: '100', unit: '%', label: 'On-device option' },
  { value: '0', label: 'Bytes uploaded in Privacy Mode' },
  { value: '10–100', unit: '×', label: 'Cheaper to run than the cloud' },
];

type Feature = {
  eyebrow: string;
  title: string;
  body: string;
  bullets: string[];
  src: string;
  slug: string;
  alt: string;
  icon: React.ReactNode;
  imageRight: boolean;
  gold?: boolean;
  tier?: 'free' | 'pro';
};

// The headline capability — speaker diarization + voice fingerprinting,
// explained in plain end-user language (no "pyannote"/"diarization" jargon
// in the customer-facing copy; the tech names live in the FAQ for credibility).
const SPEAKER_FEATURES: Feature[] = [
  {
    eyebrow: 'Who said what',
    title: 'Know exactly who said what',
    body: 'Meeting-Ops automatically splits the conversation up by speaker, so every line of the transcript is tagged with the person who said it. Your recap reads like a script — no more “wait, who said that?”',
    bullets: [
      'Separates every speaker, turn by turn',
      'Every line labeled with a name',
      'Holds up even when people talk over each other',
    ],
    src: 'sessions',
    slug: '/sessions',
    alt: 'Meeting-Ops transcript with every line labeled by the person who said it',
    icon: <Users className="h-5 w-5" />,
    imageRight: true,
    tier: 'pro',
  },
  {
    eyebrow: 'It remembers voices',
    title: 'It learns your team’s voices',
    body: 'The first time it hears someone, Meeting-Ops quietly learns their voice — a private “voiceprint.” After that it recognizes them automatically: “Speaker 2” becomes “Dana,” in this meeting and every one after. Name a regular once and they name themselves forever.',
    bullets: [
      'Recognizes recurring people by their voice',
      'Auto-labels everyone across all your meetings',
      'Confirm a name once — it remembers from then on',
    ],
    src: 'speakers',
    slug: '/speakers',
    alt: 'Meeting-Ops speaker library — voice profiles recognized across meetings with match confidence',
    icon: <Fingerprint className="h-5 w-5" />,
    imageRight: false,
    gold: true,
    tier: 'pro',
  },
];

const FEATURES: Feature[] = [
  {
    eyebrow: 'Live capture',
    title: 'Watch the transcript form as you talk',
    body: 'The moment you start, words appear — and a rolling summary keeps pace, so you can stay in the conversation instead of scribbling notes. It all runs in your browser on WebGPU, with models cached locally.',
    bullets: [
      'Live transcript as you speak — no bot, no upload',
      'Real-time rolling summary',
      'Always-on capture with smart silence gaps',
    ],
    src: 'recording',
    slug: '/record',
    alt: 'Meeting-Ops live recording screen — a transcript forming next to a live AI summary',
    icon: <Mic className="h-5 w-5" />,
    imageRight: true,
    tier: 'free',
  },
  {
    eyebrow: 'The record',
    title: 'The meeting, understood',
    body: 'When you stop, Meeting-Ops delivers the finished record: an executive summary, the key decisions, action items with owners and due dates, and who spoke how much. The follow-up email writes itself — because it already did.',
    bullets: [
      'Executive summary, decisions, and topics',
      'Action items with owners and due dates',
      'Speaking-time and participation breakdown',
    ],
    src: 'session-summary',
    slug: '/sessions',
    alt: 'Meeting-Ops session summary — executive summary, key decisions, and action items with owners',
    icon: <FileDown className="h-5 w-5" />,
    imageRight: false,
    gold: true,
    tier: 'pro',
  },
  {
    eyebrow: 'Ask your meetings',
    title: 'Chat with everything you’ve ever discussed',
    body: 'Ask a question and get an answer drawn from your entire library — "What did we decide about pricing?" or "Summarize everything Dana committed to this quarter." It reads across meetings, cites its sources, and runs on our own models.',
    bullets: [
      'Cross-meeting answers, with citations',
      'Semantic search across your whole library',
      'Action items & decisions, answered on the models we run',
    ],
    src: 'aichat',
    slug: '/chat',
    alt: 'Meeting-Ops AI chat answering a question across multiple past meetings',
    icon: <MessageSquare className="h-5 w-5" />,
    imageRight: true,
    tier: 'pro',
  },
  {
    eyebrow: 'Institutional memory',
    title: 'Your meetings become a knowledge graph',
    body: 'Every meeting feeds a person-centric graph that connects people, topics, decisions, and follow-ups across time. Six months from now, "why did we choose this pricing?" is one question away — with the meetings to back it up.',
    bullets: [
      'People, topics, and decisions, connected across meetings',
      'See how a decision evolved conversation by conversation',
      'AI agents can query it over MCP, with permission',
    ],
    src: 'knowledge-graph',
    slug: '/knowledge',
    alt: 'Meeting-Ops knowledge graph connecting people, topics, and decisions across meetings',
    icon: <Fingerprint className="h-5 w-5" />,
    imageRight: false,
    tier: 'pro',
  },
];

// The platform's real depth, beyond the headline features (all true to code).
const CAPABILITIES: Array<{ icon: React.ReactNode; title: string; body: string }> = [
  {
    icon: <FileDown className="h-5 w-5" />,
    title: 'Export anything',
    body: 'PDF, DOCX, Markdown, SRT, TXT — plus spoken audio summaries and AI-drafted follow-up emails.',
  },
  {
    icon: <Upload className="h-5 w-5" />,
    title: 'Bring your back-catalog',
    body: 'Drag in hundreds of old recordings; titles and dates auto-parse, then everything is transcribed and summarized.',
  },
  {
    icon: <Radio className="h-5 w-5" />,
    title: 'Rooms & always-on',
    body: 'Set up a conference room with a fixed mic or satellite device for hands-free, always-on capture.',
  },
  {
    icon: <CalendarDays className="h-5 w-5" />,
    title: 'Period digests',
    body: 'One AI rollup of everything that happened this day, week, month, or quarter.',
  },
  {
    icon: <BookOpen className="h-5 w-5" />,
    title: 'Custom vocabulary',
    body: 'Teach it your product names, acronyms, and people so transcripts get them right.',
  },
  {
    icon: <Plug className="h-5 w-5" />,
    title: 'Connect your stack',
    body: 'Push action items to your project tracker, build a knowledge graph, and let AI agents query meetings over MCP.',
  },
];

const FAQS: Array<{ q: string; a: React.ReactNode }> = [
  {
    q: 'Where does the processing actually happen?',
    a: 'Live transcription and summaries run on-device, in your browser, using local models. In Privacy Mode, your audio never leaves your machine. If you opt into the Pro server pass, by default it runs on GPUs we operate — not handed off to a third-party AI.',
  },
  {
    q: 'Do you train your models on my meetings?',
    a: 'No. We never train on your conversations, transcripts, or summaries. The server pass processes your audio to give you a result and nothing more.',
  },
  {
    q: 'Who actually runs the server pass — is it OpenAI or AssemblyAI?',
    a: 'By default, no. The server pass runs on infrastructure we operate: Parakeet for transcription, pyannote for diarization, and our own Qwen model for summaries — not OpenAI, Anthropic, AssemblyAI, or Deepgram. If your team would rather bring its own provider key, you can opt into that per workspace — but it’s your choice, and never the default.',
  },
  {
    q: 'Can I bring my own AI provider key (OpenAI, Anthropic, …)?',
    a: 'Yes. A workspace admin can point the language model at their own provider — OpenAI, Anthropic, OpenRouter, and more — with the key encrypted at rest. It’s entirely opt-in; out of the box, the whole stack is self-hosted and nothing goes to a third party.',
  },
  {
    q: 'What’s free, and what costs money?',
    a: `The full live experience is free, forever: on-device transcription, rolling summaries, and your complete session history — no card required. Pro (${PRO_MONTHLY_PRICE_WITH_PERIOD}) adds the server pass, polished AI summaries, cross-meeting chat, semantic search, and the knowledge graph.`,
  },
  {
    q: 'How accurate is the transcription?',
    a: 'The on-device live transcript is built for speed and real-time follow-along. The optional server pass uses studio-grade Parakeet 1.1B transcription and pyannote 3.1 diarization to produce a clean, labeled, finished record.',
  },
  {
    q: 'Can I self-host or run this on-prem?',
    a: 'Yes — and you can own the whole thing. Meeting-Ops is open-source and self-hostable, so regulated and enterprise teams can run the entire stack — server pass included — on their own infrastructure: cloud, on-prem, or appliance.',
  },
  {
    q: 'How is this different from Otter, Granola, or Fathom?',
    a: 'They stream your audio to third-party transcription services and send your words to OpenAI or Anthropic, every minute. Meeting-Ops runs live transcription on your device and, by default, the server pass on the models and GPUs we operate — so out of the box nothing goes to a third-party AI. That also makes it far cheaper to run (and free to start).',
  },
  {
    q: 'Is it secure and compliant enough for enterprise or healthcare?',
    a: 'Yes. Because audio can stay fully on-device and, by default, the server pass runs only on infrastructure we operate, Meeting-Ops fits HIPAA-conscious and enterprise environments — with self-host and on-prem options when you need full control over where your data lives.',
  },
];

// ---------------------------------------------------------------------------
// FAQ accordion item — native <details>/<summary> (no JS, a11y by default)
// ---------------------------------------------------------------------------

const FaqItem: React.FC<{ q: string; a: React.ReactNode }> = ({ q, a }) => (
  <details name="faq" className="group py-5">
    <summary className="flex cursor-pointer list-none items-center justify-between gap-4 rounded-md text-left font-display text-lg text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/50 [&::-webkit-details-marker]:hidden">
      {q}
      <Plus className="h-5 w-5 shrink-0 text-gold-400 transition-transform duration-300 group-open:rotate-45 motion-reduce:transition-none" />
    </summary>
    <div className="mt-3 max-w-2xl text-base leading-relaxed text-zinc-400">{a}</div>
  </details>
);

// ---------------------------------------------------------------------------
// Lightbox — full-size screenshot overlay (Esc / backdrop / button to close)
// ---------------------------------------------------------------------------

const Lightbox: React.FC<{ src: string; alt: string; onClose: () => void }> = ({
  src,
  alt,
  onClose,
}) => {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={alt}
      onClick={onClose}
      className="animate-fadeIn fixed inset-0 z-[60] flex items-center justify-center bg-navy-950/90 p-4 backdrop-blur-sm sm:p-8"
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="absolute right-4 top-4 flex h-11 w-11 items-center justify-center rounded-lg text-zinc-300 ring-1 ring-white/15 transition hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/60"
      >
        <X className="h-6 w-6" />
      </button>
      <figure className="max-h-full">
        <img
          src={src}
          alt={alt}
          onClick={(e) => e.stopPropagation()}
          className="mx-auto max-h-[84vh] max-w-[94vw] rounded-xl object-contain shadow-2xl shadow-black/70 ring-1 ring-white/15"
        />
        <figcaption className="mt-3 text-center font-mono text-[11px] uppercase tracking-wider text-zinc-500">
          {alt}
        </figcaption>
      </figure>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Feature row — a text column + a framed (clickable) screenshot, alternating
// ---------------------------------------------------------------------------

const FeatureRow: React.FC<{
  f: Feature;
  onEnlarge: (src: string, alt: string) => void;
}> = ({ f, onEnlarge }) => (
  <Reveal>
    <div className="grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
      <div className={f.imageRight ? 'lg:order-1' : 'lg:order-2'}>
        <div className="flex items-center gap-2">
          <span className="text-gold-400">{f.icon}</span>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-gold-400">
            {f.eyebrow}
          </p>
          {f.tier && (
            <span
              className={`ml-1 rounded-full px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider ${
                f.tier === 'free'
                  ? 'bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-500/30'
                  : 'bg-gold-500/15 text-gold-300 ring-1 ring-gold-500/30'
              }`}
            >
              {f.tier}
            </span>
          )}
        </div>
        <h3 className="mt-4 font-display text-3xl font-semibold tracking-[-0.01em] text-white sm:text-4xl">
          {f.title}
        </h3>
        <p className="mt-5 text-lg leading-relaxed text-zinc-400">{f.body}</p>
        <ul className="mt-6 space-y-3">
          {f.bullets.map((b) => (
            <li key={b} className="flex items-start gap-3 text-zinc-300">
              <Check className="mt-0.5 h-5 w-5 shrink-0 text-gold-400" />
              <span>{b}</span>
            </li>
          ))}
        </ul>
      </div>
      <div
        className={`${
          f.imageRight ? 'lg:order-2' : 'lg:order-1'
        } order-first lg:order-none`}
      >
        <FramedShot
          src={f.src}
          slug={f.slug}
          alt={f.alt}
          gold={f.gold}
          onEnlarge={onEnlarge}
        />
      </div>
    </div>
  </Reveal>
);

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export const Landing: React.FC = () => {
  const [email, setEmail] = useState('');
  const [state, setState] = useState<SubmitState>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(null);

  const openLightbox = useCallback((src: string, alt: string) => {
    setLightbox({ src, alt });
  }, []);

  const scrollToId = useCallback((id: string) => {
    const el = document.getElementById(id);
    if (!el) return;
    const reduce =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
  }, []);

  // Pro CTA — real checkout. Stripe Checkout needs a JWT, so an anonymous
  // visitor is routed to /signup?intent=pro (hash router); a signed-in visitor
  // goes straight to Checkout. Auth is read from the stored token rather than
  // AuthContext so this page stays decoupled from the provider (it renders for
  // anonymous marketing visitors and in tests without an AuthProvider).
  const handleGetPro = useCallback(() => {
    track('landing_pro_cta', {});
    if (hasStoredAuthToken()) {
      void beginProCheckout('landing');
    } else {
      window.location.hash = '/signup?intent=pro';
    }
  }, []);

  // ── Invite funnel (UNCHANGED behaviour — covered by Landing.test.tsx) ──
  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setErrorMsg(null);

    const trimmed = email.trim();
    if (!trimmed || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
      setErrorMsg('Please enter a valid email address.');
      setState('error');
      return;
    }

    setState('submitting');
    // Full-path CTA: always fire-and-forget the invite-request log (so
    // manual follow-up still has the email captured + the existing
    // rate-limit + notify path keeps working), then immediately redirect
    // to /signup?email=. The Signup page detects the 403 "registration
    // closed" reply gracefully and shows the friendly invite-confirmation
    // copy when self-serve isn't open.
    try {
      const apiBase = config.apiBaseUrl.replace(/\/$/, '');
      const url = `${apiBase}/api/landing/invite-request`;
      void fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: trimmed }),
      }).catch(() => {
        /* intentional: the redirect below is the user-facing path */
      });
      track('landing_invite_requested', {});
      const encoded = encodeURIComponent(trimmed);
      // Hash router — setting location.hash to `/signup?email=...` makes
      // the URL `<origin>/#/signup?email=...`, which is what HashRouter reads.
      window.location.hash = `/signup?email=${encoded}`;
      setState('success');
      setEmail('');
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Network error.');
      setState('error');
    }
  };

  const primaryCta = BETA ? 'Request early access' : 'Start free';

  return (
    <div className="relative min-h-screen overflow-x-hidden bg-navy-950 font-sans text-zinc-300 antialiased selection:bg-gold-500/20 selection:text-gold-200">
      {/* Fixed engineering dot-grid, densest behind the hero, fading down */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 -z-20 [background-image:radial-gradient(rgba(255,255,255,0.06)_1px,transparent_1px)] [background-size:32px_32px] [mask-image:radial-gradient(ellipse_70%_55%_at_50%_0%,#000_55%,transparent_100%)]"
      />

      <Nav onAnchor={scrollToId} />

      <main>
        {/* ───────────────────────── Hero ───────────────────────── */}
        <section className="relative isolate px-6 pb-24 pt-32 sm:pt-36 md:pb-28 md:pt-40 lg:px-8">
          <Aurora className="left-1/2 top-0 h-[520px] w-[820px] max-w-[95vw] -translate-x-1/2" gold />
          {/* Officer-unicorn watermark ghosted behind the headline. Low
              opacity + desaturation keep the rainbow mane from fighting the
              headline contrast; the radial mask fades it out before it
              reaches the subhead. Decorative only. */}
          <img
            src="/brand/meeting-ops-mark.png?v=3"
            alt=""
            aria-hidden="true"
            draggable={false}
            className="pointer-events-none absolute left-1/2 top-14 -z-10 w-[20rem] -translate-x-1/2 select-none opacity-[0.10] saturate-[0.7] sm:top-10 sm:w-[26rem] lg:w-[30rem] [mask-image:radial-gradient(ellipse_62%_62%_at_50%_40%,#000_30%,transparent_76%)]"
          />
          <div className="mx-auto max-w-3xl text-center">
            <div className="inline-flex items-center gap-2 rounded-full bg-white/5 px-3 py-1 ring-1 ring-white/10">
              <Waveform className="h-3.5" />
              <span className="font-mono text-xs uppercase tracking-[0.18em] text-gold-300">
                {BETA
                  ? 'Private beta · request an invite'
                  : 'Meeting intelligence · free to start · open source'}
              </span>
            </div>

            <h1 className="mt-7 font-display text-5xl font-semibold leading-[1.05] tracking-[-0.02em] text-white sm:text-6xl lg:text-7xl">
              {HERO_HEADLINE.pre}
              <span className="bg-gradient-to-r from-gold-200 via-gold-400 to-gold-300 bg-clip-text text-transparent">
                {HERO_HEADLINE.wink}
              </span>
              {HERO_HEADLINE.post}
            </h1>

            <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed text-zinc-400 sm:text-xl">
              The AI meeting recorder that transcribes live in your browser,
              then delivers a studio-grade record — who said what, the
              decisions, the action items — searchable across every meeting
              you’ve ever had. Open source if you’d rather self-host, and
              Privacy Mode keeps a conversation entirely on your device when
              it can’t leave the room.
            </p>

            <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button type="button" onClick={() => scrollToId('invite')} className={btnPrimary}>
                {primaryCta}
                <ArrowRight className="h-4 w-4" />
              </button>
              <button type="button" onClick={() => scrollToId('how')} className={btnSecondary}>
                See a meeting become work
              </button>
            </div>
            <p className="mt-5 font-mono text-[11px] tracking-wide text-zinc-500">
              Free tier, forever · No credit card · Open source · Privacy Mode built in
            </p>
          </div>

          {/* Hero product shot */}
          <div className="mx-auto mt-16 max-w-6xl [mask-image:linear-gradient(to_bottom,#000_84%,transparent)] md:mt-20">
            <FramedShot
              src="dashboard"
              slug="/dashboard"
              alt="Meeting-Ops dashboard — recent meetings, extracted action items, and on-device AI status"
              priority
              onEnlarge={openLightbox}
            />
          </div>

          {/* Compact pricing teaser — the full cards live in #pricing */}
          <div className="mx-auto mt-10 max-w-3xl text-center">
            <button
              type="button"
              onClick={() => scrollToId('pricing')}
              className="group inline-flex flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-full bg-white/[0.04] px-5 py-2.5 ring-1 ring-white/10 transition hover:ring-gold-500/40"
            >
              <span className="text-sm text-zinc-300">
                Start locally for <span className="font-semibold text-emerald-300">free</span>
              </span>
              <span className="text-zinc-600">·</span>
              <span className="text-sm text-zinc-300">
                Add server intelligence for{' '}
                <span className="font-mono text-zinc-500 line-through">$20</span>{' '}
                <span className="font-semibold text-gold-300">$15/mo</span>
              </span>
              <span className="text-sm text-zinc-500 transition group-hover:text-gold-300">
                See pricing →
              </span>
            </button>
          </div>
        </section>

        {/* ─────────────────────── Trust strip ─────────────────────── */}
        <section className="border-y border-white/5 bg-white/[0.015] py-8">
          <div className="mx-auto max-w-5xl px-6">
            <p className="text-center font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-500">
              Built for teams that care where their audio goes
            </p>
            <div className="mt-5 flex flex-wrap items-center justify-center gap-x-4 gap-y-3">
              {TRUST_CHIPS.map((c) => (
                <span
                  key={c.label}
                  className="inline-flex items-center gap-2 rounded-full bg-white/[0.04] px-3.5 py-1.5 text-sm text-zinc-300 ring-1 ring-white/10"
                >
                  {c.icon}
                  {c.label}
                </span>
              ))}
            </div>
          </div>
        </section>

        {/* ───────────────────── How it works ───────────────────── */}
        <section id="how" className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="mx-auto max-w-7xl">
            <Reveal>
              <SectionHeader
                eyebrow="How it works"
                title="From mic to memory in three steps"
                sub="Record locally, get a clean record, then ask your meetings anything."
              />
            </Reveal>
            <div className="relative mt-16 grid gap-6 md:grid-cols-3">
              <div
                aria-hidden="true"
                className="absolute inset-x-0 top-[2.4rem] hidden h-px bg-gradient-to-r from-transparent via-white/10 to-transparent md:block"
              />
              {STEPS.map((s, i) => (
                <Reveal key={s.n} delay={i * 80}>
                  <div className="relative h-full rounded-2xl bg-white/[0.03] p-7 ring-1 ring-white/10 transition hover:bg-white/[0.05] hover:ring-white/20">
                    <div className="flex items-center justify-between">
                      <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-navy-800 text-gold-300 ring-1 ring-white/10">
                        {s.icon}
                      </span>
                      <span className="font-mono text-sm text-gold-400/70">{s.n}</span>
                    </div>
                    <h3 className="mt-5 font-display text-xl font-medium tracking-tight text-white">
                      {s.title}
                    </h3>
                    <p className="mt-2 text-base leading-relaxed text-zinc-400">{s.body}</p>
                  </div>
                </Reveal>
              ))}
            </div>
          </div>
        </section>

        {/* ──────────────────── The privacy / moat ──────────────────── */}
        <section
          id="privacy"
          className="relative isolate scroll-mt-24 overflow-hidden bg-black px-6 py-28 md:py-36 lg:px-8"
        >
          <Aurora className="left-1/2 top-0 h-[560px] w-[900px] max-w-[95vw] -translate-x-1/2" gold />
          <div className="mx-auto max-w-7xl">
            <Reveal>
              <SectionHeader
                eyebrow="The difference"
                title={
                  <>
                    Other recorders resell someone else’s AI.{' '}
                    <span className="text-gold-300">We run our own — or yours.</span>
                  </>
                }
                sub="Most meeting AI streams your audio to a third-party service and pipes your words into OpenAI — every minute, all meeting long. Meeting-Ops does the live work right in your browser, runs the studio pass on hardware we operate, and the whole stack is open source if you’d rather run it yourself."
              />
            </Reveal>

            {/* Them vs Us */}
            <Reveal className="mt-16">
              <div className="relative grid gap-px overflow-hidden rounded-3xl bg-white/5 ring-1 ring-white/10 md:grid-cols-2">
                <div
                  aria-hidden="true"
                  className="absolute bottom-8 left-1/2 top-8 hidden w-px -translate-x-1/2 bg-gradient-to-b from-transparent via-gold-500/40 to-transparent md:block"
                />
                {/* THEM */}
                <div className="bg-navy-950/60 p-8 sm:p-10">
                  <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-zinc-500">
                    Cloud recorders
                  </span>
                  <h3 className="mt-2 font-display text-2xl text-zinc-300">
                    Your audio leaves the room
                  </h3>
                  <ul className="mt-6 space-y-4">
                    {THEM.map((t) => (
                      <li key={t} className="flex items-start gap-3 text-zinc-500">
                        <X className="mt-0.5 h-5 w-5 shrink-0 text-zinc-600" />
                        <span>{t}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                {/* US */}
                <div className="bg-navy-900 p-8 ring-1 ring-gold-500/30 sm:p-10">
                  <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-gold-400">
                    Meeting-Ops
                  </span>
                  <h3 className="mt-2 font-display text-2xl text-white">
                    Your audio stays on your device
                  </h3>
                  <ul className="mt-6 space-y-4">
                    {US.map((u) => (
                      <li key={u} className="flex items-start gap-3 text-zinc-200">
                        <Check className="mt-0.5 h-5 w-5 shrink-0 text-gold-400" />
                        <span>{u}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </Reveal>

            {/* Proof stats */}
            <div className="mt-12 grid grid-cols-2 gap-6 md:grid-cols-4">
              {STATS.map((s, i) => (
                <Reveal key={s.label} delay={i * 70}>
                  <div className="h-full rounded-2xl bg-white/[0.03] p-6 text-center ring-1 ring-white/10">
                    <div className="font-display text-4xl font-semibold tabular-nums text-white">
                      {s.value}
                      {s.unit && (
                        <span className={s.gold ? 'text-gold-400' : 'text-gold-400'}>
                          {s.unit}
                        </span>
                      )}
                    </div>
                    <div className="mt-2 font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
                      {s.label}
                    </div>
                  </div>
                </Reveal>
              ))}
            </div>
          </div>
        </section>

        {/* ─────────────── Speaker intelligence (highlighted) ─────────────── */}
        <section id="speakers" className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="mx-auto max-w-7xl">
            <Reveal>
              <SectionHeader
                eyebrow="Speaker intelligence"
                title="It doesn’t just transcribe — it knows who’s talking"
                sub="Two things competitors fumble and we make effortless: separating the speakers, and recognizing the same people meeting after meeting. The result is a named, labeled record of who said and decided what — automatically."
              />
            </Reveal>
            <div className="mt-20 space-y-24 md:space-y-32">
              {SPEAKER_FEATURES.map((f) => (
                <FeatureRow key={f.src} f={f} onEnlarge={openLightbox} />
              ))}
            </div>
          </div>
        </section>

        {/* ─────────────────────── Features ─────────────────────── */}
        <section id="features" className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="mx-auto max-w-7xl">
            <Reveal>
              <SectionHeader
                eyebrow="The product"
                title="Capture live, recall forever"
                sub="Free on-device capture, plus an optional studio-grade pass that turns a meeting into a searchable, connected record you can ask anything."
              />
            </Reveal>

            <div className="mt-20 space-y-24 md:space-y-32">
              {FEATURES.map((f) => (
                <FeatureRow key={f.src} f={f} onEnlarge={openLightbox} />
              ))}
            </div>

            {/* The work keeps moving — Project-Ops handoff, human-approved */}
            <Reveal className="mt-24">
              <div className="rounded-3xl bg-gradient-to-b from-navy-900 to-navy-950 p-8 ring-1 ring-gold-500/20 sm:p-10">
                <div className="flex items-center gap-2">
                  <span className="rounded-full bg-gold-500/15 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-gold-300 ring-1 ring-gold-500/30">
                    Pro
                  </span>
                  <p className="font-mono text-xs uppercase tracking-[0.2em] text-gold-400">
                    The work keeps moving
                  </p>
                </div>
                <h3 className="mt-3 font-display text-2xl font-medium text-white sm:text-3xl">
                  A commitment made out loud becomes a task on a board.
                </h3>
                <p className="mt-3 max-w-2xl leading-relaxed text-zinc-400">
                  Meeting-Ops extracts action items and proposes them to
                  Project-Ops — nothing is assigned until a human reviews and
                  approves it. The loop closes when the decision becomes
                  tracked work.
                </p>
                <div className="mt-7 flex flex-wrap items-center gap-2 font-mono text-xs sm:text-sm">
                  {['“I’ll own the draft by Friday”', 'Extracted', 'Proposed', 'Human-approved', 'Project-Ops task'].map(
                    (step, i, arr) => (
                      <React.Fragment key={step}>
                        <span
                          className={`rounded-lg px-3 py-2 ring-1 ${
                            i === 0
                              ? 'bg-white/[0.04] italic text-zinc-300 ring-white/10'
                              : i === arr.length - 1
                                ? 'bg-gold-500/15 font-semibold text-gold-300 ring-gold-500/30'
                                : 'bg-white/[0.04] text-zinc-300 ring-white/10'
                          }`}
                        >
                          {step}
                        </span>
                        {i < arr.length - 1 && (
                          <ArrowRight className="h-4 w-4 shrink-0 text-zinc-600" />
                        )}
                      </React.Fragment>
                    ),
                  )}
                </div>
              </div>
            </Reveal>

            {/* Bento: settings shot + deploy-anywhere card */}
            <Reveal className="mt-24">
              <div className="grid gap-6 md:grid-cols-3">
                <div className="overflow-hidden rounded-3xl bg-white/[0.03] p-6 ring-1 ring-white/10 md:col-span-2">
                  <p className="font-mono text-xs uppercase tracking-[0.2em] text-gold-400">
                    Made for real orgs
                  </p>
                  <h3 className="mt-3 font-display text-2xl font-medium text-white">
                    Configure it your way
                  </h3>
                  <p className="mt-2 max-w-xl text-base leading-relaxed text-zinc-400">
                    Vocabulary, models, export formats, retention — all in your
                    hands. Export anything to PDF, DOCX, TXT, or SRT. No lock-in.
                  </p>
                  <div className="mt-6">
                    <FramedShot
                      src="settings"
                      slug="/settings"
                      alt="Meeting-Ops settings — models, vocabulary, export, and retention controls"
                      onEnlarge={openLightbox}
                    />
                  </div>
                </div>
                <div className="flex flex-col rounded-3xl bg-gradient-to-b from-navy-900 to-navy-950 p-7 ring-1 ring-white/10">
                  <div className="flex items-center gap-2">
                    <Github className="h-5 w-5 text-gold-400" />
                    <p className="font-mono text-xs uppercase tracking-[0.2em] text-gold-400">
                      Open source · self-host
                    </p>
                  </div>
                  <h3 className="mt-3 font-display text-2xl font-medium text-white">
                    Or run it entirely yourself
                  </h3>
                  <p className="mt-2 text-base leading-relaxed text-zinc-400">
                    Want full control? Meeting-Ops is open-source and
                    self-hostable — run the whole stack on your own hardware.
                  </p>
                  <ul className="mt-5 space-y-3 text-zinc-300">
                    {['Self-host the full stack', 'HIPAA & enterprise ready', 'Your data, your hardware'].map(
                      (b) => (
                        <li key={b} className="flex items-start gap-3">
                          <Check className="mt-0.5 h-5 w-5 shrink-0 text-gold-400" />
                          <span>{b}</span>
                        </li>
                      ),
                    )}
                  </ul>
                  <div className="mt-auto pt-8">
                    <Waveform className="h-6 w-full opacity-30" />
                  </div>
                </div>
              </div>
            </Reveal>
          </div>
        </section>

        {/* ───────────── More capabilities + platforms ───────────── */}
        <section className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="mx-auto max-w-7xl">
            <Reveal>
              <SectionHeader
                eyebrow="More in the box"
                title="A platform, not just a transcript"
                sub="The headline features are only the start. Meeting-Ops is a full meeting workspace."
              />
            </Reveal>
            <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {CAPABILITIES.map((c, i) => (
                <Reveal key={c.title} delay={(i % 3) * 70}>
                  <div className="h-full rounded-2xl bg-white/[0.03] p-6 ring-1 ring-white/10 transition hover:bg-white/[0.05] hover:ring-white/20">
                    <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-navy-800 text-gold-300 ring-1 ring-white/10">
                      {c.icon}
                    </span>
                    <h3 className="mt-4 font-display text-lg font-medium text-white">{c.title}</h3>
                    <p className="mt-2 text-sm leading-relaxed text-zinc-400">{c.body}</p>
                  </div>
                </Reveal>
              ))}
            </div>

            {/* Platforms — web, installable PWA, native iOS */}
            <Reveal className="mt-12">
              <div className="rounded-3xl bg-gradient-to-b from-navy-900 to-navy-950 p-8 ring-1 ring-white/10 sm:p-10">
                <div className="flex flex-col items-center gap-8 text-center md:flex-row md:items-center md:justify-between md:text-left">
                  <div className="max-w-xl">
                    <p className="font-mono text-xs uppercase tracking-[0.2em] text-gold-400">
                      Record anywhere
                    </p>
                    <h3 className="mt-2 font-display text-2xl font-medium text-white sm:text-3xl">
                      In your browser, installed as an app — and on iPhone
                    </h3>
                    <p className="mt-3 leading-relaxed text-zinc-400">
                      Capture from any modern browser or install Meeting-Ops to
                      your home screen today. The native app with on-device
                      transcription is free on the App Store — iPhone, iPad,
                      Mac, and Apple Vision.
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center justify-center gap-3">
                    {[
                      { icon: <Globe className="h-4 w-4 text-gold-400" />, label: 'Web', href: undefined as string | undefined },
                      { icon: <Smartphone className="h-4 w-4 text-gold-400" />, label: 'Installable app', href: undefined as string | undefined },
                      { icon: <Apple className="h-4 w-4 text-gold-400" />, label: 'iOS · App Store', href: 'https://apps.apple.com/us/app/meeting-ops/id6780018348' },
                    ].map((p) =>
                      p.href ? (
                        <a
                          key={p.label}
                          href={p.href}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-2 rounded-full bg-gold-500/15 px-4 py-2 text-sm font-medium text-gold-300 ring-1 ring-gold-500/30 transition hover:bg-gold-500/25"
                        >
                          {p.icon}
                          {p.label}
                        </a>
                      ) : (
                        <span
                          key={p.label}
                          className="inline-flex items-center gap-2 rounded-full bg-white/[0.04] px-4 py-2 text-sm font-medium text-zinc-200 ring-1 ring-white/10"
                        >
                          {p.icon}
                          {p.label}
                        </span>
                      )
                    )}
                  </div>
                </div>
              </div>
            </Reveal>
          </div>
        </section>

        {/* ─────────────────────── Pricing ─────────────────────── */}
        <section id="pricing" className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="mx-auto max-w-7xl">
            <Reveal>
              <SectionHeader
                eyebrow="Pricing"
                title="Free forever. Pay only for the heavy lifting."
                sub="The live, on-device experience is free for good. Pro adds the GPU server pass and the intelligence layer across your whole library."
              />
            </Reveal>

            <div className="mx-auto mt-14 grid max-w-4xl gap-6 md:grid-cols-2">
              {/* Free */}
              <Reveal>
                <div className="flex h-full flex-col rounded-3xl bg-white/[0.03] p-8 ring-1 ring-white/10">
                  <h3 className="font-display text-xl font-semibold text-white">Free</h3>
                  <p className="mt-1 text-sm text-zinc-400">
                    Everything you need to never take meeting notes again.
                  </p>
                  <div className="mt-5 flex items-end gap-1">
                    <span className="font-display text-5xl font-semibold tabular-nums text-white">
                      $0
                    </span>
                    <span className="pb-1 text-sm text-zinc-500">/forever</span>
                  </div>
                  <ul className="mt-6 space-y-3 text-sm text-zinc-300">
                    {[
                      'On-device live transcript + rolling summary',
                      'Full session history, searchable',
                      'Runs in your browser — private by default',
                      'No credit card, no time limit',
                    ].map((b) => (
                      <li key={b} className="flex items-start gap-2.5">
                        <Check className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" />
                        <span>{b}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-auto pt-8">
                    <button
                      type="button"
                      onClick={() => scrollToId('invite')}
                      className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-transparent px-5 py-3 text-sm font-semibold text-gold-300 ring-1 ring-gold-500/40 transition hover:bg-gold-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold-500/60"
                    >
                      {BETA ? 'Request access' : 'Start free'}
                    </button>
                    <p className="mt-3 text-center text-xs text-zinc-500">
                      No card required. Your audio never leaves your device.
                    </p>
                  </div>
                </div>
              </Reveal>

              {/* Pro */}
              <Reveal delay={80}>
                <div className="relative flex h-full flex-col rounded-3xl bg-navy-900 p-8 shadow-[0_0_60px_-15px_rgba(212,175,55,0.25)] ring-1 ring-gold-500/40">
                  <span className="absolute -top-3 right-6 rounded-full bg-gold-500 px-3 py-1 font-mono text-[10px] uppercase tracking-widest text-navy-950">
                    Most popular
                  </span>
                  <h3 className="font-display text-xl font-semibold text-white">Pro</h3>
                  <p className="mt-1 text-sm text-zinc-400">
                    Studio-quality output and an AI that knows your whole library.
                  </p>
                  <div className="mt-5 flex items-end gap-2">
                    <span className="pb-1 font-display text-2xl font-medium text-zinc-500 line-through decoration-zinc-600">
                      {PRO_LIST_PRICE_DISPLAY}
                    </span>
                    <span className="font-display text-5xl font-semibold tabular-nums text-white">
                      {PRO_MONTHLY_PRICE_DISPLAY}
                    </span>
                    <span className="pb-1 text-sm text-zinc-500">/month</span>
                  </div>
                  <p className="mt-1 text-xs font-semibold text-gold-400">
                    Launch pricing — {PRO_LAUNCH_DISCOUNT_PCT}% off
                  </p>
                  <ul className="mt-6 space-y-3 text-sm text-zinc-200">
                    {[
                      'Everything in Free',
                      'Server pass: studio transcripts + speaker diarization',
                      'Polished AI summaries with action items & decisions',
                      'Cross-meeting AI chat + semantic search',
                      'Person-centric knowledge graph',
                    ].map((b) => (
                      <li key={b} className="flex items-start gap-2.5">
                        <Check className="mt-0.5 h-4 w-4 shrink-0 text-gold-400" />
                        <span>{b}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-auto pt-8">
                    <button
                      type="button"
                      onClick={handleGetPro}
                      className={`${btnPrimary} w-full`}
                    >
                      Get Pro
                      <ArrowRight className="h-4 w-4" />
                    </button>
                    <p className="mt-3 text-center text-xs text-zinc-500">
                      Cancel anytime · We never sell or train on your data ·{' '}
                      <a
                        href="#/terms"
                        className="text-gold-300 underline-offset-4 hover:underline"
                      >
                        Terms
                      </a>
                    </p>
                  </div>
                </div>
              </Reveal>
            </div>

            <p className="mt-8 text-center font-mono text-[11px] text-zinc-500">
              Need on-prem, an appliance, or a HIPAA BAA?{' '}
              <a
                href="https://unicorncommander.ai/pricing"
                target="_blank"
                rel="noopener noreferrer"
                className="text-gold-300 underline-offset-4 hover:underline"
              >
                See full pricing on the Unicorn Commander storefront →
              </a>
            </p>
          </div>
        </section>

        {/* ─────────────────────────── FAQ ─────────────────────────── */}
        <section id="faq" className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="mx-auto max-w-3xl">
            <Reveal>
              <SectionHeader
                eyebrow="Questions"
                title="Everything you might ask"
                sub="Straight answers on privacy, pricing, accuracy, and where your data actually goes."
              />
            </Reveal>
            <Reveal className="mt-12">
              <div className="divide-y divide-white/10 border-y border-white/10">
                {FAQS.map((f) => (
                  <FaqItem key={f.q} q={f.q} a={f.a} />
                ))}
              </div>
            </Reveal>
          </div>
        </section>

        {/* ──────────────────── Final CTA + invite ──────────────────── */}
        <section id="invite" className="scroll-mt-24 px-6 py-24 md:py-32 lg:px-8">
          <div className="relative isolate mx-auto max-w-5xl overflow-hidden rounded-3xl bg-gradient-to-b from-navy-900 to-navy-950 px-6 py-16 text-center ring-1 ring-white/10 sm:px-10 md:py-24">
            <Aurora className="left-1/2 top-0 h-[360px] w-[640px] max-w-[90%] -translate-x-1/2" gold />
            <span className="mx-auto flex h-12 w-12 items-center justify-center overflow-hidden rounded-xl bg-navy-900 ring-1 ring-gold-500/30">
              <img
                src="/brand/meeting-ops-mark.png?v=3"
                alt="Meeting-Ops"
                className="h-full w-full object-cover"
              />
            </span>
            <h2 className="mx-auto mt-6 max-w-2xl font-display text-4xl font-semibold tracking-[-0.02em] text-white sm:text-5xl">
              It doesn’t just remember what was said.{' '}
              <span className="bg-gradient-to-r from-gold-200 via-gold-400 to-gold-300 bg-clip-text text-transparent">
                It makes sure something happens because of it.
              </span>
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-lg leading-relaxed text-zinc-400">
              {BETA
                ? 'Request early access to the private beta — start free and on-device, and upgrade to studio-quality output whenever you want.'
                : 'Start with the free, on-device experience in your browser — and upgrade to studio-quality output the day you want it.'}
            </p>

            {state === 'success' ? (
              <div
                role="status"
                aria-live="polite"
                className="mx-auto mt-8 max-w-md rounded-xl border border-emerald-700/40 bg-emerald-900/20 px-4 py-3 text-sm text-emerald-300"
              >
                Taking you to account setup…
              </div>
            ) : (
              <form
                onSubmit={handleSubmit}
                className="mx-auto mt-8 flex max-w-md flex-col gap-3 sm:flex-row"
                noValidate
              >
                <label htmlFor="invite-email" className="sr-only">
                  Email address
                </label>
                <input
                  id="invite-email"
                  type="email"
                  required
                  autoComplete="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    if (state === 'error') setState('idle');
                  }}
                  disabled={state === 'submitting'}
                  className="flex-1 rounded-xl border border-white/15 bg-navy-950 px-4 py-3 text-sm text-zinc-100 outline-none transition placeholder:text-zinc-500 focus:border-gold-500/60 focus:ring-2 focus:ring-gold-500/30 disabled:opacity-60"
                />
                <button
                  type="submit"
                  disabled={state === 'submitting'}
                  className={`${btnPrimary} shrink-0 disabled:opacity-60`}
                >
                  {state === 'submitting' ? 'Submitting…' : 'Continue'}
                </button>
              </form>
            )}

            {state === 'error' && errorMsg && (
              <div
                role="alert"
                className="mx-auto mt-4 max-w-md rounded-xl border border-rose-700/40 bg-rose-900/20 px-4 py-3 text-sm text-rose-300"
              >
                {errorMsg}
              </div>
            )}

            <p className="mt-5 font-mono text-[11px] tracking-wide text-zinc-500">
              {BETA
                ? 'We’re onboarding teams in waves — no spam, just your invite.'
                : 'No credit card, and your audio never leaves your device.'}
            </p>
          </div>
        </section>

        {/* ─────────────────────────── Footer ─────────────────────────── */}
        <footer className="border-t border-white/10 bg-navy-950 px-6 pb-10 pt-16 lg:px-8">
          <div className="mx-auto grid max-w-7xl gap-10 md:grid-cols-[1.6fr_1fr_1fr]">
            <div>
              <div className="flex items-center gap-2.5">
                <span className="flex h-8 w-8 items-center justify-center overflow-hidden rounded-lg bg-navy-900 ring-1 ring-gold-500/30">
                  <img
                    src="/brand/meeting-ops-mark.png?v=3"
                    alt="Meeting-Ops"
                    className="h-full w-full object-cover"
                  />
                </span>
                <span className="font-display text-lg font-semibold tracking-tight text-white">
                  Meeting-Ops
                </span>
              </div>
              <p className="mt-4 max-w-xs text-sm leading-relaxed text-zinc-500">
                Intelligence for your conversations — whether or not they are. An
                AI meeting recorder that processes on your device.
              </p>
              <p className="mt-4 font-mono text-[11px] text-zinc-600">
                Built by Magic Unicorn Unconventional Technology &amp; Stuff Inc.
              </p>
            </div>

            <div>
              <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
                Product
              </p>
              <ul className="mt-3 space-y-2 text-sm">
                {NAV_LINKS.map(([label, id]) => (
                  <li key={id}>
                    <button
                      type="button"
                      onClick={() => scrollToId(id)}
                      className="text-zinc-400 transition-colors hover:text-white"
                    >
                      {label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
                Company
              </p>
              <ul className="mt-3 space-y-2 text-sm">
                <li>
                  <a href="#/login" className="text-zinc-400 transition-colors hover:text-white">
                    Sign in
                  </a>
                </li>
                <li>
                  <a href="#/contact" className="text-zinc-400 transition-colors hover:text-white">
                    Contact support
                  </a>
                </li>
                <li>
                  <a
                    href="mailto:enterprise@meeting-ops.unicorncommander.ai"
                    className="text-zinc-400 transition-colors hover:text-white"
                  >
                    Enterprise &amp; on-prem
                  </a>
                </li>
              </ul>
            </div>
          </div>

          <LegalFooter />
        </footer>
      </main>

      {lightbox && (
        <Lightbox
          src={lightbox.src}
          alt={lightbox.alt}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  );
};

export default Landing;
