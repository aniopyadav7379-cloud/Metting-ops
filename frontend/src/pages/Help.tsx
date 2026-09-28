import React, { useState } from 'react';
import {
  Cpu,
  Server,
  Shield,
  ShieldCheck,
  Globe,
  Zap,
  Clock,
  ArrowRight,
  Mic,
  Brain,
  Lock,
  Smartphone,
  User,
  Users,
  Radio,
  Circle,
  FolderTree,
  FileText,
  MessageSquare,
  Download,
  Search,
  Building2,
  Database,
  Tag,
  Network,
  KeyRound,
  RefreshCw,
  Sparkles,
  Plug,
  Eye,
} from 'lucide-react';

/**
 * Help page — role-based explainer for how Meeting-Ops actually works.
 *
 * Three progressively-deeper "reads" for three audiences, switched with a
 * segmented control and defaulting to the End-user tier:
 *
 *   1. For everyone (End user)         — how to use it day to day.
 *   2. For team admins (Admin/Manager) — running an org: speakers, members,
 *                                         integrations, tiers, reprocess.
 *   3. For administrators (Operator)   — the operational/architecture model.
 *
 * The substance underneath all of it: live work runs in your browser, the
 * polish pass runs on our servers when the meeting ends, privacy mode keeps
 * everything local. The full strategy doc lives at docs/compute-economics.md.
 */

type Tier = 'everyone' | 'admin' | 'operator';

interface TierMeta {
  id: Tier;
  label: string;
  audience: string;
  icon: React.ComponentType<{ className?: string }>;
}

const TIERS: TierMeta[] = [
  { id: 'everyone', label: 'For everyone', audience: 'Anyone using Meeting-Ops', icon: User },
  { id: 'admin', label: 'For team admins', audience: 'Admins & managers', icon: Users },
  { id: 'operator', label: 'For administrators', audience: 'Operators & superusers', icon: ShieldCheck },
];

export default function Help() {
  const [tier, setTier] = useState<Tier>('everyone');
  const active = TIERS.find((t) => t.id === tier) ?? TIERS[0];

  return (
    <div className="h-full overflow-y-auto bg-zinc-950 px-4 py-6 md:px-8 md:py-8">
      <div className="mx-auto max-w-3xl space-y-8">
        <header>
          <h1 className="text-2xl font-semibold text-zinc-100 md:text-3xl">
            How Meeting-Ops works
          </h1>
          <p className="mt-3 text-zinc-400">
            How to use it, where your audio goes, and how it's built. Pick the
            read that fits you — each tab goes a little deeper than the one
            before it.
          </p>
        </header>

        {/* ---------------------- Tier switcher (tabs) --------------------- */}
        <div
          role="tablist"
          aria-label="Choose a help section by role"
          className="grid grid-cols-1 gap-2 sm:grid-cols-3"
        >
          {TIERS.map((t) => {
            const Icon = t.icon;
            const isActive = t.id === tier;
            return (
              <button
                key={t.id}
                role="tab"
                aria-selected={isActive}
                onClick={() => setTier(t.id)}
                className={`flex items-start gap-3 rounded-lg border p-3 text-left transition-colors ${
                  isActive
                    ? 'border-fuchsia-500/60 bg-fuchsia-500/10'
                    : 'border-zinc-800 bg-zinc-900/40 hover:border-zinc-700 hover:bg-zinc-900'
                }`}
              >
                <Icon
                  className={`mt-0.5 h-5 w-5 flex-shrink-0 ${
                    isActive ? 'text-fuchsia-300' : 'text-zinc-500'
                  }`}
                />
                <span>
                  <span
                    className={`block text-sm font-semibold ${
                      isActive ? 'text-fuchsia-200' : 'text-zinc-200'
                    }`}
                  >
                    {t.label}
                  </span>
                  <span className="mt-0.5 block text-xs text-zinc-500">
                    {t.audience}
                  </span>
                </span>
              </button>
            );
          })}
        </div>

        {/* Active-tier header */}
        <div className="flex items-center gap-3 border-b border-zinc-800 pb-3">
          <active.icon className="h-5 w-5 text-fuchsia-300" />
          <div>
            <h2 className="text-lg font-semibold text-zinc-100">{active.label}</h2>
            <p className="text-sm text-zinc-500">{active.audience}</p>
          </div>
        </div>

        {tier === 'everyone' && <EveryoneTier />}
        {tier === 'admin' && <AdminTier />}
        {tier === 'operator' && <OperatorTier />}

        {/* ------------------------- Shared footer ------------------------ */}
        <section className="space-y-3 border-t border-zinc-800 pt-6">
          <h2 className="text-lg font-semibold text-zinc-100">Still stuck?</h2>
          <p className="text-sm text-zinc-400">
            Talk to a human. Tell us what you ran into and we'll reply on your
            account email.{' '}
            <a
              href="#/contact"
              className="text-fuchsia-300 underline-offset-4 hover:underline"
            >
              Contact Support →
            </a>
          </p>
        </section>
      </div>
    </div>
  );
}

/* ===================================================================== */
/* Tier 1 — For everyone (End user)                                      */
/* ===================================================================== */

function EveryoneTier() {
  return (
    <div className="space-y-10">
      <p className="text-zinc-300">
        The short version: hit <span className="text-zinc-100">Record</span>, talk
        through your meeting, tap Stop. A minute later you get a clean
        transcript, a summary, and a list of action items in your sessions. The
        rest of this page is the detail — nothing here is required reading to
        get started.
      </p>

      <section className="space-y-4">
        <SectionHeader icon={Mic} title="Recording a meeting" />
        <p className="text-zinc-300">
          Go to <span className="text-zinc-100">Record</span> and pick how you
          want to capture audio, then hit record. The live transcript and a
          rolling summary build as you talk; tap Stop when you're done and the
          finished transcript + summary land in your sessions.
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Circle className="mt-1 h-3 w-3 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Just me</span> — captures your
              microphone only. Good for in-person meetings and dictation.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Circle className="mt-1 h-3 w-3 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Me + system audio</span> — also
              captures what's playing on your computer (the other people on a
              video call, a webinar). Your browser will ask you to share a tab or
              screen <em>with audio</em> — keep that box checked.
            </span>
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Zap} title="Browser recording = full quality" iconClass="text-amber-400" />
        <p className="text-zinc-300">
          Recording straight in your browser now runs the exact same transcription
          and speaker-identification pipeline as uploading an audio file. There's
          no quality trade-off for hitting Record instead of importing — whichever
          way the audio gets in, you get the same finished transcript, the same
          "who said what," and the same summary.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Download} title="Uploading an existing recording" />
        <p className="text-zinc-300">
          Already have an audio file? Use{' '}
          <span className="text-zinc-100">Import / Export</span> in the sidebar to
          upload one (or many) existing recordings. They run through the same
          processing as a live meeting — transcript, speakers, summary, action
          items — and show up in Sessions when they finish.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={FileText} title="Where everything ends up: reading a meeting" />
        <p className="text-zinc-300">
          Open any session to get the playback plus everything the meeting
          produced:
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <FileText className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Transcript</span> with speaker
              labels, searchable and filterable by speaker, synced to the audio
              player.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Brain className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Summary, key points, and action
              items</span> generated from the full transcript. Action items pull
              out who-owns-what so nothing gets lost.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <MessageSquare className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Ask about this meeting</span> — a
              chat panel that answers questions using this meeting's transcript as
              context ("what did we decide about pricing?").
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Download className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Export</span> the transcript or
              summary as PDF, DOCX, TXT, or SRT, and download the audio.
            </span>
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Users} title="Who said what — automatic speaker identification" />
        <p className="text-zinc-300">
          Meeting-Ops separates every voice in a recording (this is called{' '}
          <span className="text-zinc-100">diarization</span>) and matches each one
          against the people it has heard before. Anyone it recognizes is labeled
          by name automatically — you don't have to do anything.
        </p>
        <p className="text-zinc-300">
          Every voice gets a <span className="text-zinc-100">stable identity</span>{' '}
          that sticks with that person from meeting to meeting. Once a person is
          known, they're recognized in future meetings on their own. A voice it
          hasn't met yet shows up under a steady handle like{' '}
          <span className="text-zinc-100">"Speaker 3F2A"</span> — not a throwaway
          "Speaker 1" that changes every time — so you can give it a name once and
          have it stick.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Tag} title="Name a speaker once — it fixes every meeting, instantly" />
        <p className="text-zinc-300">
          When you open a meeting with unnamed voices, a highlighted{' '}
          <span className="text-zinc-100">"name the speakers"</span> prompt sits
          right at the top of the record — pick who each voice is from the dropdown
          (it pre-fills its best guess, so it's usually one tap each).
        </p>
        <p className="text-zinc-300">
          The moment you name a speaker, that name shows up{' '}
          <span className="text-zinc-100">everywhere that person has spoken</span> —
          across every past meeting's transcript, not just this one — right away,
          with no waiting and nothing to re-run. The name is shown live from that
          person's profile, so it's correct everywhere at once. Meeting summaries
          for those past meetings get updated to use the real name too.
        </p>
        <p className="text-sm text-zinc-400">
          Want a past meeting's summary rewritten now that you've named someone?
          Open the <span className="text-zinc-200">Speakers</span> page, pick the
          person, and choose{' '}
          <span className="text-zinc-200">Re-summarize past meetings</span> to
          regenerate their earlier summaries on demand.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={ShieldCheck} title="Confirming a speaker is safe" iconClass="text-emerald-400" />
        <p className="text-zinc-300">
          Each time you confirm or correct who a voice belongs to, you're teaching
          the system what that person sounds like, so it recognizes them more
          reliably next time. Confirming only ever{' '}
          <span className="text-zinc-100">improves</span> accuracy — it can't make
          things worse.
        </p>
        <p className="text-zinc-300">
          There's a built-in safeguard for that: a consistency floor that quietly
          ignores a segment that doesn't actually sound like the person — a
          mislabeled clip, or one where two voices overlap. That means a stray bad
          segment can never corrupt someone's saved voice, no matter what you
          confirm. So go ahead and confirm freely.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={FolderTree} title="Finding meetings: Sessions & Folders" />
        <p className="text-zinc-300">
          The <span className="text-zinc-100">Sessions</span> page lists every
          meeting with full-text search and filters. Switch to the{' '}
          <span className="text-zinc-100">Folders</span> view to group them.
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Tag className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              Folders <span className="text-zinc-100">are tags</span> — so a
              single meeting can live in several folders at once (e.g. a client
              folder <em>and</em> a project folder). The rail shows each folder
              with a session count, plus "All sessions" and "Ungrouped".
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Search className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              Make a folder with <span className="text-zinc-100">New folder</span>,
              then select one or more sessions and{' '}
              <span className="text-zinc-100">Add to folder</span>. Remove them the
              same way. The flat list, search, and filters are always still there.
            </span>
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Sparkles} title="Search & AI Chat across all your meetings" />
        <p className="text-zinc-300">
          Two ways to dig back through everything you've recorded:
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Search className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Search</span> on the Sessions page
              is full-text — type a word or phrase and it finds every meeting
              where it was said, with the matching lines.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <MessageSquare className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">AI Chat</span> (the chat icon in the
              sidebar) lets you <em>ask</em> across all your meetings in plain
              language — "what were the open action items from my calls with
              Acme last month?" It reads the relevant transcripts, then answers
              and cites which meetings it pulled from. This is a back-and-forth
              conversation, so you can keep narrowing in.
            </span>
          </li>
        </ul>
        <p className="text-sm text-zinc-400">
          <Clock className="-mt-0.5 mr-1 inline h-3.5 w-3.5" />
          AI Chat only sees meetings you have access to, and only searches once
          a meeting has finished processing.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Network} title="Knowledge Graph: how a person connects across meetings" />
        <p className="text-zinc-300">
          The <span className="text-zinc-100">Knowledge Graph</span> page (when
          your organization has it turned on) is a visual map of people, meetings,
          topics, and action items and how they link together. Click a person to
          see every meeting they appeared in, what they're connected to, and the
          threads that run between conversations — useful when you're trying to
          remember "where did this come up before, and who was in the room?"
        </p>
        <p className="text-sm text-zinc-400">
          Don't see it in the sidebar? It's an optional feature your admin
          enables per organization — there's nothing to install on your end.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Lock} title="Your recordings, your device — Privacy mode" iconClass="text-emerald-400" />
        <p className="text-zinc-300">
          Meeting-Ops can run entirely on your own computer. With{' '}
          <span className="text-zinc-100">Privacy mode</span> on (the lock icon in
          the recorder), the audio is processed{' '}
          <span className="text-zinc-100">entirely in your browser</span> and never
          leaves your device — the transcript, the speaker identification, and the
          summary are all produced right there on your machine and stored locally.
          Nothing is uploaded.
        </p>
        <p className="text-zinc-300">
          Those recordings live under{' '}
          <span className="text-zinc-100">Local Sessions</span> in the sidebar
          (the entry appears after your first one). They're yours, on this device:
          you can read, search, and export them as Markdown or JSON, but they
          aren't synced to other machines because nothing was uploaded.
        </p>
        <p className="text-sm text-zinc-400">
          On the free tier this is the <em>only</em> mode — everything stays on
          your machine by default. More on what that means in the other tabs.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Download} title="Exporting" />
        <p className="text-zinc-300">
          Every meeting can be exported. From a session, use{' '}
          <span className="text-zinc-100">Export</span> for the transcript or
          summary as PDF, DOCX, TXT, or SRT, and to download the original audio.
          Local (privacy-mode) sessions export as Markdown or JSON — Markdown is
          the readable artifact; JSON can optionally include the audio so you can
          move a session between machines.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={RefreshCw} title="Troubleshooting: my meeting didn't appear" />
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-300">
          <p>
            After you tap Stop, the meeting processes{' '}
            <span className="text-zinc-100">in the background</span> on our servers
            for roughly 30 to 90 seconds — cleaning up the transcript, sorting out
            who said what, and writing the summary. During that window you'll see
            a "Reprocessing…" banner.
          </p>
          <ul className="mt-3 space-y-1.5 text-zinc-400">
            <li>
              <span className="text-zinc-200">Give it a minute</span>, then hit{' '}
              <span className="text-zinc-200">Refresh</span> on the Sessions page.
              It usually shows up on its own.
            </li>
            <li>
              <span className="text-zinc-200">Summary looks thin?</span> The live
              summary you saw while recording is a draft; the polished one lands
              with the background pass.
            </li>
            <li>
              <span className="text-zinc-200">Recording on a phone?</span> The
              whole recording uploads when you tap Stop, so on a slow connection
              the meeting can take a little longer to appear.
            </li>
            <li>
              <span className="text-zinc-200">Still missing after a few
              minutes?</span> Reach out via Contact Support at the bottom of this
              page.
            </li>
          </ul>
        </div>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Smartphone} title="On your phone or tablet" iconClass="text-sky-400" />
        <p className="text-zinc-300">
          Modern phones and tablets (recent iPhone/iPad on iOS 18+, recent Android
          Chrome) run the same on-device live transcript and summary as a laptop —
          nothing streams while you record, and the polished version lands in your
          sessions about a minute after you tap Stop. Older phones simply record
          and let the server do the transcript and summary at Stop. Either way you
          get the same finished result.
        </p>
      </section>
    </div>
  );
}

/* ===================================================================== */
/* Tier 2 — For team admins (Admin / Manager)                            */
/* ===================================================================== */

function AdminTier() {
  return (
    <div className="space-y-10">
      <p className="text-zinc-300">
        This tier is for whoever runs Meeting-Ops for a team or organization —
        the person who manages speakers and members, decides which integrations
        are on, and picks the plan. These controls live in{' '}
        <span className="text-zinc-100">Settings</span> and on the Speakers page,
        and are visible to organization admins and managers.
      </p>

      <section className="space-y-4">
        <SectionHeader icon={Users} title="Speaker Library: enroll, merge, and link to users" />
        <p className="text-zinc-300">
          The <span className="text-zinc-100">Speakers</span> page is where the
          "who said what" gets accurate over time. When a meeting is processed,
          the system separates the voices (diarization) and matches them against
          everyone you've enrolled.
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Mic className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Enroll a speaker</span> by naming a
              voice the system found, or by confirming a suggested match. Once
              enrolled, that person is auto-labeled in future meetings whose audio
              matches their voiceprint with high confidence.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Users className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Merge speakers</span> when the same
              person shows up as two entries (e.g. picked up separately in two
              meetings). Merging folds their voiceprints together so matching gets
              stronger. The page also surfaces an{' '}
              <span className="text-zinc-100">Unassigned</span> group for voices
              that haven't been named yet.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <User className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Link a speaker to a member</span> so
              their meetings, action items, and graph connections tie back to a
              real user account in your organization.
            </span>
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Building2} title="Organizations & workspace switching" />
        <p className="text-zinc-300">
          Meeting-Ops is multi-tenant: every meeting, speaker, folder, and setting
          belongs to an <span className="text-zinc-100">organization</span>. If you
          belong to more than one (e.g. your company plus a personal space), the{' '}
          <span className="text-zinc-100">organization switcher</span> changes
          which workspace you're looking at — sessions, speakers, and settings all
          follow the active org. Your role can differ per org, so you might be an
          admin in one and a regular member in another.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Plug} title="Per-organization integrations" />
        <p className="text-zinc-300">
          Under <span className="text-zinc-100">Settings → Integrations</span>,
          admins flip integrations on or off <em>per organization</em>. They ship
          off by default, and credentials are stored encrypted at rest. The main
          ones:
        </p>
        <div className="space-y-3">
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
              <Network className="h-4 w-4 text-fuchsia-400" />
              Knowledge graph
            </div>
            <p className="mt-1 text-sm text-zinc-400">
              Writes meetings, speakers, and action items into the shared
              knowledge graph so the Knowledge Graph page can show how people and
              topics connect across conversations.
            </p>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
              <ArrowRight className="h-4 w-4 text-fuchsia-400" />
              Project-Ops push
            </div>
            <p className="mt-1 text-sm text-zinc-400">
              Sends action items from a meeting into Project-Ops as tasks. Opt-in
              per organization — when off, nothing is pushed.
            </p>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
              <Users className="h-4 w-4 text-fuchsia-400" />
              Contact-Ops
            </div>
            <p className="mt-1 text-sm text-zinc-400">
              Resolves meeting attendees to real people in Contact-Ops, so a
              meeting can be keyed to the contacts it actually involved.
            </p>
          </div>
        </div>
        <p className="text-sm text-zinc-400">
          Each toggle is scoped to the organization you're currently in — turning
          one on for one org doesn't affect any other.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={RefreshCw} title="Reprocessing a meeting" />
        <p className="text-zinc-300">
          If a meeting's transcript, speakers, or summary need another pass — say
          after you've enrolled a new speaker, or the first pass was rough — admins
          and managers can <span className="text-zinc-100">reprocess</span> it. That
          re-runs the server pipeline (transcribe → diarize → summarize → index for
          search) against the stored audio and updates the session in place.
        </p>
        <p className="text-sm text-zinc-400">
          <Clock className="-mt-0.5 mr-1 inline h-3.5 w-3.5" />
          Reprocessing is paced through a fair queue so a burst of requests can't
          starve live recordings or run the box hot — large batches may trickle
          through rather than all finishing at once.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Users} title="Inviting & managing members" />
        <p className="text-zinc-300">
          Admins manage who's in an organization and what they can do. Invite
          people into the org, set their role (admin / manager / member), and
          remove them when they leave. Roles decide who can see admin-only
          surfaces like the Speaker Library configuration, integration toggles,
          and reprocessing — regular members get the recording and reading
          experience without those controls.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Sparkles} title="Tiers & plans — what each unlocks" />
        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="text-sm font-medium text-zinc-100">Free</div>
            <div className="mt-1 text-sm text-zinc-400">
              Fully on-device in your browser. Live transcript + summary,
              recordings stay local under Local Sessions. Nothing uploaded.
            </div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="text-sm font-medium text-zinc-100">Pro</div>
            <div className="mt-1 text-sm text-zinc-400">
              Adds the server quality pass (better transcript + diarization +
              summary), the live server transcript, durable cross-device storage,
              cross-meeting AI Chat, and bulk import.
            </div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="text-sm font-medium text-zinc-100">Enterprise</div>
            <div className="mt-1 text-sm text-zinc-400">
              On-prem / private deployment, Privacy mode on by default, and
              HIPAA-suitable handling for regulated environments.
            </div>
          </div>
        </div>
        <p className="text-sm text-zinc-400">
          The plan is set per organization, so the same person can be on different
          tiers in different orgs. Server-side features (reprocessing, the quality
          pass, cross-device storage, bulk import) only light up on paid tiers —
          free organizations stay fully browser-side.
        </p>
      </section>
    </div>
  );
}

/* ===================================================================== */
/* Tier 3 — For administrators (Superuser / Operator)                    */
/* ===================================================================== */

function OperatorTier() {
  return (
    <div className="space-y-10">
      <p className="text-zinc-300">
        This tier is the architecture-and-operations read for whoever runs the
        deployment. It's intentionally high-level — enough to reason about where
        compute happens, what the server does, and where the levers are, without
        repeating the deploy runbooks. The full strategy doc lives at{' '}
        <code className="rounded bg-zinc-900 px-1 py-0.5 text-zinc-300">
          docs/compute-economics.md
        </code>
        .
      </p>

      <section className="space-y-4">
        <SectionHeader icon={Cpu} title="Browser-first compute model" />
        <p className="text-zinc-300">
          The defining design choice: live transcription and live summarization
          run in the <span className="text-zinc-100">user's browser</span>, not on
          a server. There is no third-party transcription API in the live loop.
          The first time someone opens the recorder, a couple of small models are
          downloaded and cached in the browser, and subsequent meetings start
          instantly.
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Mic className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Live transcript</span> — Parakeet
              0.6B INT8, on the user's CPU (or GPU via WebGPU where available).
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Brain className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Live summary</span> — a small LM
              (Qwen 3 0.6B by default, Gemma 4 E2B as an alternative), refreshing
              every few hundred words.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Zap className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Cost model</span> — marginal compute
              cost per concurrent user-hour is effectively $0 server-side, because
              the client does the real-time work. Default new features to
              browser-side compute; only fold something into the server pass if
              quality genuinely requires it.
            </span>
          </li>
        </ul>
        <p className="text-sm text-zinc-400">
          Exact file sizes, device classes, and what-runs-where are in{' '}
          <code className="rounded bg-zinc-900 px-1 py-0.5 text-zinc-300">
            docs/browser-models.md
          </code>
          .
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Server} title="The reprocess pipeline (server completion pass)" />
        <p className="text-zinc-300">
          On a paid tier, when a meeting ends the audio gets a single server pass —
          the "quality pass" — that produces the canonical artifacts. The same
          pass runs on an explicit reprocess. The stages, in order:
        </p>
        <ol className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-zinc-800 text-xs font-semibold text-zinc-300">
              1
            </span>
            <span>
              <span className="text-zinc-100">Transcribe</span> — Parakeet 1.1B
              (a larger sibling of the in-browser model) with word-level
              timestamps.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-zinc-800 text-xs font-semibold text-zinc-300">
              2
            </span>
            <span>
              <span className="text-zinc-100">Diarize</span> — pyannote 3.1 plus
              speaker embeddings, matched against the org's Speaker Library to
              attach known identities.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-zinc-800 text-xs font-semibold text-zinc-300">
              3
            </span>
            <span>
              <span className="text-zinc-100">Summarize</span> — Qwen 3.6
              35B-A3B-Vision (a vision-capable Mixture-of-Experts model, far
              larger than the browser ran), producing the polished summary, key
              points, and action items. The same model backs cross-meeting AI
              Chat.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-zinc-800 text-xs font-semibold text-zinc-300">
              4
            </span>
            <span>
              <span className="text-zinc-100">Index to search</span> — the
              transcript is embedded and indexed so it's reachable from full-text
              search and cross-meeting AI Chat (RAG). If graph integration is on,
              meeting/speaker/action-item nodes are written at this point too.
            </span>
          </li>
        </ol>
        <p className="text-sm text-zinc-400">
          <Clock className="-mt-0.5 mr-1 inline h-3.5 w-3.5" />
          One pass at the end is deliberate — streaming every word through a large
          model continuously would mean per-minute cost and constant audio
          egress. Reprocess jobs run through a bounded fair queue with a daily soft
          cap so they can't starve live recordings.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Database} title="Embeddings & reranking on the shared Infinity server" iconClass="text-emerald-400" />
        <p className="text-zinc-300">
          The retrieval side of search and AI Chat — turning transcript text into
          embeddings, and reranking candidate passages before they're handed to
          the LM — now runs on the{' '}
          <span className="text-zinc-100">shared Infinity inference server</span>
          rather than inside the app process. The app calls out to it for those
          two jobs (embed, rerank); keeping them on a shared service means the
          model loads once and is reused across the suite instead of being
          duplicated per app. Vector storage and the hybrid (dense + sparse)
          search index are unchanged.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Radio} title="Server live transcript (paid tier)" />
        <p className="text-zinc-300">
          On pro / enterprise tiers, the Record page shows a second transcript
          pane fed by a separate streaming pipeline that sends audio to the GPU:
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Mic className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Per-word streaming</span> via NVIDIA
              Parakeet realtime EOU 120M — emits words as the model commits them,
              with end-of-utterance detection rather than a fixed cadence.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <Users className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Live speaker labels</span> via NVIDIA
              Sortformer 4-speaker streaming diarization, in parallel with the
              transcript.
            </span>
          </li>
        </ul>
        <p className="text-sm text-zinc-400">
          Gated on the <code className="rounded bg-zinc-900 px-1 py-0.5 text-zinc-300">server_live</code>{' '}
          feature. The browser transcript stays the privacy-respecting baseline;
          the server pane is the higher-fidelity live view. See{' '}
          <code className="rounded bg-zinc-900 px-1 py-0.5 text-zinc-300">
            docs/runbook-streaming-v2.md
          </code>
          .
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Eye} title="Session watchdog" />
        <p className="text-zinc-300">
          A periodic watchdog reconciles session state so the system self-heals.
          It fails out sessions that have been stuck in a recording/processing
          state far longer than any real meeting (a tab left open, a dropped
          client) so they don't sit half-finished forever. It uses heartbeats
          rather than a blunt age cutoff, so genuinely long-running active
          recordings are left alone — important after an earlier bug where an
          age-only sweep cut live sessions short.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={KeyRound} title="MCP & Personal Access Tokens" />
        <p className="text-zinc-300">
          Meeting-Ops exposes its data to external agents two ways:
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <Plug className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">MCP server</span> — a Model Context
              Protocol server giving agents read tools (search meetings, ask
              across meetings, get details/transcript/insights) plus the
              tier-gated write paths. This is how other suite apps and assistants
              reach meeting data.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <KeyRound className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Personal Access Tokens</span> —
              issued from{' '}
              <span className="text-zinc-100">Settings → Personal Access Tokens</span>
              , these are scoped, revocable credentials for programmatic and
              MCP access that act as a specific user. Treat them like passwords.
            </span>
          </li>
        </ul>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Shield} title="Privacy & regulated deployments" iconClass="text-emerald-400" />
        <p className="text-zinc-300">
          <span className="text-zinc-100">Privacy mode</span> is per-session and
          skips the server pass entirely — the audio bytes are never uploaded,
          which is exact and verifiable. The browser instead runs a full-quality
          transcription and summary pass over the locally-held recording so the
          output matches the server pipeline in shape. Enterprise / clinical
          deployments default Privacy mode on for HIPAA-suitable handling.
        </p>
        <p className="text-zinc-300">
          Finished (non-privacy) recordings are stored in our own object storage
          on our own hardware — not a third-party cloud, and deleting a session
          purges its audio. Nothing is shipped to AWS, Google, OpenAI, or
          Anthropic by default.
        </p>
      </section>

      <section className="space-y-4">
        <SectionHeader icon={Globe} title="Where the settings live" />
        <p className="text-zinc-300">
          Operational knobs are split between deploy-time and the app:
        </p>
        <ul className="space-y-2 text-zinc-300">
          <li className="flex items-start gap-3">
            <ArrowRight className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Settings (in-app)</span> — providers,
              in-browser AI model choice, vocabulary, integrations, and Personal
              Access Tokens. Admin-only sections are hidden from non-admins
              entirely.
            </span>
          </li>
          <li className="flex items-start gap-3">
            <ArrowRight className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-500" />
            <span>
              <span className="text-zinc-100">Deploy environment</span> — tier
              behavior, feature kill-switches (e.g. the Knowledge Graph page and
              Room mode are env-gated, default-off where there's no hardware for
              them), reprocess enablement and caps, and per-org plan all live in
              configuration rather than the UI.
            </span>
          </li>
        </ul>
        <p className="text-sm text-zinc-400">
          Endpoints, env var names, and the deploy runbook are intentionally not
          duplicated here — keep them in the source tree (
          <code className="rounded bg-zinc-900 px-1 py-0.5 text-zinc-300">
            docs/README.md
          </code>{' '}
          is the canonical index) where they stay accurate.
        </p>
      </section>
    </div>
  );
}

/* ===================================================================== */
/* Shared little section header                                          */
/* ===================================================================== */

function SectionHeader({
  icon: Icon,
  title,
  iconClass = 'text-fuchsia-400',
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  iconClass?: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <Icon className={`h-5 w-5 ${iconClass}`} />
      <h3 className="text-lg font-semibold text-zinc-100">{title}</h3>
    </div>
  );
}
