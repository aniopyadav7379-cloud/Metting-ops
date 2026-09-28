import { lazy, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { BadgeCheck, ExternalLink, Mic, RefreshCw, Trash2 } from 'lucide-react';
import { config } from '../config';
import { useAuth } from '../contexts/AuthContext';
import { useOrg } from '../contexts/OrgContext';
import { isAdminRole } from '../utils/roles';
import { deleteMyVoice, getMyVoice } from '../utils/api';
import type { MyVoiceStatus } from '../utils/api';
import { showToast } from '../components/Toast';
import SettingsLayout from '../components/settings/SettingsLayout';
import type { SaveStatus } from '../components/settings/SettingsLayout';
import {
  ALL_SECTIONS,
  APPLIANCE_ONLY_SECTIONS,
  PLACEHOLDER_SECTIONS,
  SettingsProvider,
  SETTINGS_STORAGE_KEY,
  loadStoredSettings,
} from '../components/settings/SettingsContext';
import type {
  HostCapabilities,
  SettingsState,
} from '../components/settings/SettingsContext';

// Same admin shape as the router — superuser OR per-org admin. Used to
// hide adminOnly settings sections from regular users (Aaron: "hide it
// entirely, don't show with a disabled gray state").

const AudioSettings = lazy(() => import('../components/settings/AudioSettings'));
const AISettings = lazy(() => import('../components/settings/AISettings'));
const VocabularySettings = lazy(
  () => import('../components/settings/VocabularySettings')
);
const ProvidersSettings = lazy(
  () => import('../components/settings/ProvidersSettings')
);
const InBrowserAISettings = lazy(
  () => import('../components/settings/InBrowserAISettings')
);
const NetworkSettings = lazy(
  () => import('../components/settings/NetworkSettings')
);
const WifiSettings = lazy(() => import('../components/settings/WifiSettings'));
const PerformanceSettings = lazy(
  () => import('../components/settings/PerformanceSettings')
);
const PersonalAccessTokens = lazy(
  () => import('../components/settings/PersonalAccessTokens')
);
const ThemeSettings = lazy(() => import('../components/settings/ThemeSettings'));
const PrivacySettings = lazy(() => import('../components/settings/PrivacySettings'));
const EmptyPanel = lazy(() => import('../components/settings/EmptyPanel'));
const IntegrationsPanel = lazy(() => import('../components/settings/IntegrationsPanel'));
const ReportBrandingPanel = lazy(
  () => import('../components/settings/ReportBrandingPanel')
);
const InviteCodesCard = lazy(() => import('../components/settings/InviteCodesCard'));
const BillingSettings = lazy(() => import('../components/settings/BillingSettings'));

/**
 * "My Voice" card — personal voiceprint status + delete-on-demand.
 *
 * The voiceprint is user-scoped (follows the ACCOUNT, not the
 * workspace), so it lives next to the other personal-data controls in
 * the Privacy section. Reads GET /api/me/voice, clears via
 * DELETE /api/me/voice behind an in-app confirm (no native confirm()).
 */
function MyVoiceCard() {
  const [voice, setVoice] = useState<MyVoiceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      setVoice(await getMyVoice());
    } catch {
      setVoice(null);
    }
    setLoading(false);
  };

  useEffect(() => {
    refresh();
  }, []);

  const handleClear = async () => {
    setClearing(true);
    try {
      await deleteMyVoice();
      setConfirmOpen(false);
      showToast.success('Your voiceprint was deleted.');
      await refresh();
    } catch {
      showToast.error('Could not delete your voiceprint. Please try again.');
    }
    setClearing(false);
  };

  const updatedAt = voice?.updated_at
    ? new Date(voice.updated_at).toLocaleString()
    : null;

  return (
    <div className="rounded-lg border border-white/10 bg-zinc-900/60 p-4 sm:p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-fuchsia-500/15">
            <Mic className="h-5 w-5 text-fuchsia-300" />
          </div>
          <div>
            <h3 className="text-base font-semibold text-white">My Voice</h3>
            <p className="text-sm text-zinc-400">
              Your personal voice fingerprint.
            </p>
          </div>
        </div>
        {loading ? (
          <span className="inline-flex items-center gap-1 text-xs text-zinc-500">
            <RefreshCw className="h-3 w-3 animate-spin" />
            Loading…
          </span>
        ) : voice?.enrolled ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2.5 py-1 text-xs font-medium text-emerald-300">
            <BadgeCheck className="h-3.5 w-3.5" />
            Enrolled
          </span>
        ) : (
          <span className="inline-flex items-center rounded-full bg-zinc-700/50 px-2.5 py-1 text-xs font-medium text-zinc-400">
            Not enrolled
          </span>
        )}
      </div>

      {!loading && voice?.enrolled && (
        <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-sm text-zinc-300">
          <span>
            <span className="text-zinc-500">Voice samples:</span>{' '}
            {voice.sample_count}
          </span>
          {updatedAt && (
            <span>
              <span className="text-zinc-500">Last updated:</span> {updatedAt}
            </span>
          )}
        </div>
      )}

      <p className="mt-4 text-sm text-zinc-400">
        This is a personal voice fingerprint that follows YOUR account. It is
        only used to automatically name you in workspaces you're a member of,
        and it's deleted the moment you ask.
      </p>
      <p className="mt-2 text-sm text-zinc-400">
        How to enroll: open any meeting where you spoke and label yourself
        with &ldquo;This is me&rdquo; checked — every labeling refines it.
      </p>

      <div className="mt-4">
        <button
          onClick={() => setConfirmOpen(true)}
          disabled={loading || clearing || !voice?.enrolled}
          className="inline-flex items-center gap-1.5 rounded-lg border border-red-500/40 px-3 py-1.5 text-sm font-medium text-red-300 hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Trash2 className="h-4 w-4" />
          Clear my voiceprint
        </button>
      </div>

      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-sm rounded-lg border border-white/10 bg-zinc-900 p-5 shadow-xl">
            <h4 className="text-base font-semibold text-white">
              Clear your voiceprint?
            </h4>
            <p className="mt-2 text-sm text-zinc-400">
              This permanently deletes your personal voice fingerprint, so
              you'll no longer be auto-named in meetings. You can re-enroll
              any time by labeling yourself in a meeting where you spoke.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setConfirmOpen(false)}
                disabled={clearing}
                className="rounded-lg px-3 py-1.5 text-sm text-zinc-300 hover:bg-white/5 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleClear}
                disabled={clearing}
                className="inline-flex items-center gap-1.5 rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {clearing ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="h-4 w-4" />
                )}
                Delete voiceprint
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SettingsEnhanced() {
  const { user } = useAuth();
  const { activeOrganization } = useOrg();
  const location = useLocation();
  const isAdmin = isAdminRole(activeOrganization?.role, user?.is_superuser);
  // Default to 'audio' (the user-preferences section everyone can see).
  // We re-select to a visible section in a useEffect below in case
  // adminOnly filtering kicks out the current pick after a hostCaps /
  // role flip mid-life.
  const [activeSection, setActiveSection] = useState(isAdmin ? 'providers' : 'audio');
  const [hostCaps, setHostCaps] = useState<HostCapabilities>({
    local_audio: true,
    npu: false,
    build: 'unknown',
  });
  const [settings, setSettings] = useState<SettingsState>(loadStoredSettings);
  const [micVolume, setMicVolume] = useState(75);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch('/api/system/capabilities');
        if (res.ok) setHostCaps(await res.json());
      } catch {
        /* leave defaults */
      }
    })();
  }, []);

  useEffect(() => {
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(settings));
  }, [settings]);

  useEffect(() => {
    (async () => {
      try {
        const response = await fetch(`${config.apiUrl}/api/simple/settings`);
        if (!response.ok) return;
        const backendSettings = await response.json();
        setSettings((prev: SettingsState) => {
          const mapped: SettingsState = {
            ...prev,
            defaultDevice: backendSettings.defaultMicrophone || prev.defaultDevice,
            sampleRate: prev.sampleRate,
            transcriptionModel:
              backendSettings.transcriptionModel || prev.transcriptionModel,
            liveTranscription: backendSettings.enableLiveAI !== false,
            vadEnabled: backendSettings.enableVAD ?? prev.vadEnabled,
            // vadThreshold is a 0.0-1.0 sensitivity float (matches backend storage)
            vadThreshold: backendSettings.vadThreshold ?? prev.vadThreshold,
            enableAI: backendSettings.enableLiveAI ?? prev.enableAI,
            summaryFormat: prev.summaryFormat,
            autoGenerateActions:
              backendSettings.enableAutoSummary ?? prev.autoGenerateActions,
            speakerDiarization:
              backendSettings.enableSpeakerDiarization ?? prev.speakerDiarization,
            maxSpeakers: backendSettings.maxSpeakers || prev.maxSpeakers,
            llmModel: backendSettings.aiModel || prev.llmModel,
            llmProvider: backendSettings.aiProvider || prev.llmProvider,
            hostname: backendSettings.hostname || prev.hostname,
            ipMode: backendSettings.ipMode || prev.ipMode,
          };
          localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(mapped));
          return mapped;
        });
        setMicVolume(backendSettings.microphoneVolume ?? 75);
      } catch (error) {
        console.log('Backend settings not available, using local storage');
      }
    })();
  }, []);

  // Three filters applied in order:
  //   1. Appliance-only sections are dropped in cloud builds (existing).
  //   2. adminOnly sections are dropped entirely for non-admin users
  //      (Aaron's "hide it entirely" directive).
  // Memoized so the layout doesn't re-render the sidebar list each
  // unrelated re-render.
  const sections = useMemo(() => {
    let list = hostCaps.local_audio
      ? ALL_SECTIONS
      : ALL_SECTIONS.filter((s) => !APPLIANCE_ONLY_SECTIONS.has(s.id));
    if (!isAdmin) {
      list = list.filter((s) => !s.adminOnly);
    }
    return list;
  }, [hostCaps.local_audio, isAdmin]);

  // Deep-link bridge: `#audio-devices` (used by the dashboard's
  // first-run onboarding checklist) selects the audio section. The
  // SettingsLayout sidebar can also be cued by `?section=<id>`.
  useEffect(() => {
    const hash = location.hash.replace(/^#/, '').toLowerCase();
    const queryParams = new URLSearchParams(location.search);
    const explicit = queryParams.get('section');
    let target: string | null = null;
    if (explicit) {
      target = explicit;
    } else if (hash) {
      if (hash === 'audio-devices' || hash.startsWith('audio')) target = 'audio';
    }
    if (target && target !== activeSection) {
      setActiveSection(target);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.hash, location.search]);

  // If the active section vanishes (admin/role flip, hostCaps load),
  // re-anchor to the first visible section so the panel doesn't show
  // an "Select a setting category" empty state mid-life.
  useEffect(() => {
    if (sections.length === 0) return;
    if (!sections.some((s) => s.id === activeSection)) {
      setActiveSection(sections[0].id);
    }
  }, [sections, activeSection]);

  const handleSave = async () => {
    setSaving(true);
    setSaveStatus('idle');

    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(settings));

    const backendSettings = {
      defaultMicrophone: settings.defaultDevice,
      microphoneVolume: micVolume,
      transcriptionModel: settings.transcriptionModel,
      enableLiveAI: settings.liveTranscription,
      enableVAD: settings.vadEnabled,
      vadThreshold: settings.vadThreshold,
      enableSpeakerDiarization: settings.speakerDiarization,
      maxSpeakers: settings.maxSpeakers,
      aiModel: settings.llmModel,
      aiProvider: settings.llmProvider,
      enableAutoSummary: settings.autoGenerateActions,
      hostname: settings.hostname,
      ipMode: settings.ipMode,
      ipAddress: settings.ipAddress,
      gateway: settings.gateway,
      dns1: settings.dns1,
      dns2: settings.dns2,
      wifiEnabled: settings.wifiEnabled,
      wifiSSID: settings.wifiSSID,
      wifiPassword: settings.wifiPassword,
      defaultExportFormat: settings.defaultExportFormat,
      includeTimestamps: settings.includeTimestamps,
      includeSpeakerLabels: settings.includeSpeakerLabels,
    };

    try {
      const response = await fetch(`${config.apiUrl}/api/simple/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(backendSettings),
      });

      if (response.ok) {
        setSaveStatus('success');
        setTimeout(() => setSaveStatus('idle'), 3000);
      } else {
        throw new Error('Backend not available');
      }
    } catch (error) {
      setSaveStatus('backend-pending');
      setTimeout(() => setSaveStatus('idle'), 5000);
    } finally {
      setSaving(false);
    }
  };

  const renderActive = () => {
    switch (activeSection) {
      // ── My preferences ──
      case 'audio':
        return <AudioSettings />;
      case 'in-browser-ai':
        return <InBrowserAISettings />;
      case 'theme':
        return <ThemeSettings />;
      case 'privacy':
        return (
          <div className="space-y-6">
            <PrivacySettings />
            <MyVoiceCard />
          </div>
        );
      case 'notifications':
        return (
          <EmptyPanel
            title="Notifications"
            description="Per-meeting alerts, summary delivery, and digest cadence. Coming in a future release."
          />
        );
      case 'hotkeys':
        return (
          <EmptyPanel
            title="Hotkeys"
            description="Customizable keyboard shortcuts for start/stop/pause/marker. Coming in a future release."
          />
        );
      case 'invite-codes':
        return <InviteCodesCard />;
      case 'billing':
        return <BillingSettings />;

      // ── Recording defaults ──
      case 'recording-defaults':
        return (
          <EmptyPanel
            title="Recording defaults"
            description="Default meeting type, mic+system capture default, Free local-storage policy, Pro completion default, speaker-labeling defaults. Coming in a future release."
          />
        );
      case 'vocabulary':
        return <VocabularySettings />;

      // ── Workspace settings ──
      case 'integrations':
        return <IntegrationsPanel />;
      case 'report-branding':
        return <ReportBrandingPanel />;
      case 'calendar-sync':
        return (
          <EmptyPanel
            title="Calendar sync"
            description="Auto-create sessions from your Google / Microsoft calendar. Coming in a future release."
          />
        );
      case 'sharing-retention':
        return (
          <EmptyPanel
            title="Sharing & retention"
            description="Default sharing scope, retention windows, and export defaults. Coming in a future release."
          />
        );
      case 'speaker-library':
        return (
          <EmptyPanel
            title="Speaker library"
            description="Workspace voice enrollments and speaker labels. Coming in a future release."
          />
        );

      // ── Admin & appliance ──
      case 'providers':
        return <ProvidersSettings />;
      case 'ai':
        return <AISettings />;
      case 'network':
        return <NetworkSettings />;
      case 'wifi':
        return <WifiSettings />;
      case 'performance':
        return <PerformanceSettings />;
      case 'personal-access-tokens':
        return <PersonalAccessTokens />;
      case 'audit-export':
        return (
          <EmptyPanel
            title="Audit & export"
            description="Audit log download and bulk data export. Coming in a future release."
          />
        );

      default:
        return null;
    }
  };

  // The Save button row is suppressed for PATs (which has its own
  // create/revoke flow), placeholder shells, and the Theme panel (which
  // persists via ThemeContext on click and doesn't go through the
  // backend settings POST).
  const showSave =
    activeSection !== 'personal-access-tokens' &&
    activeSection !== 'theme' &&
    // Billing has its own Stripe-backed actions (upgrade / manage) — no
    // generic settings Save row.
    activeSection !== 'billing' &&
    !PLACEHOLDER_SECTIONS.has(activeSection);

  return (
    <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black text-zinc-100 p-6">
      <div className="max-w-7xl mx-auto">
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-white mb-2">Settings</h1>
          <p className="text-zinc-400">
            Configure Meeting-Ops hardware and software
          </p>
        </div>

        <SettingsProvider
          value={{
            settings,
            setSettings,
            micVolume,
            setMicVolume,
            hostCaps,
          }}
        >
          <SettingsLayout
            sections={sections}
            activeSection={activeSection}
            onSectionChange={setActiveSection}
            saving={saving}
            saveStatus={saveStatus}
            onSave={handleSave}
            showSave={showSave}
          >
            {renderActive()}
          </SettingsLayout>
        </SettingsProvider>

        <div className="mt-4 text-sm text-zinc-500">
          Local agent templates and model configuration:
          <a
            href="#/admin/agents"
            className="text-fuchsia-400 hover:text-fuchsia-300 ml-1 inline-flex items-center gap-1"
          >
            Agent Settings <ExternalLink className="w-3 h-3" />
          </a>
        </div>
      </div>
    </div>
  );
}
