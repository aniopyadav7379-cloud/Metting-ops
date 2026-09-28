import React, { createContext, useContext } from 'react';
import {
  Bell,
  Brain,
  BookOpen,
  Cpu,
  CreditCard,
  Download,
  FileBadge,
  Keyboard,
  KeyRound,
  Library,
  Mic,
  Monitor,
  Network,
  Palette,
  Plug,
  Settings2,
  Share2,
  ShieldCheck,
  Ticket,
  Wifi,
} from 'lucide-react';
import type { SettingsSectionMeta } from './SettingsLayout';

// Settings sections, regrouped into Aaron's four-tier IA (2026-05-29
// Codex audit, UX-A batch):
//   - "My preferences"     → per-user client-side surfaces (audio device,
//                            in-browser AI, theme, notifications,
//                            hotkeys, PATs).
//   - "Recording defaults" → defaults applied to new meetings (vocabulary,
//                            recording defaults panel).
//   - "Workspace settings" → per-org integrations, calendar, sharing,
//                            retention, speaker library.
//   - "Admin & appliance"  → providers, network, wifi, performance, legacy
//                            AI panel. adminOnly: true → hidden entirely
//                            for non-admins.
//
// UX-A scope: only regroup the existing 9 panels + add placeholder
// shells for the new groups. Don't rebuild individual panel internals.
export const ALL_SECTIONS: SettingsSectionMeta[] = [
  // ── My preferences ────────────────────────────────────────────────
  {
    id: 'audio',
    title: 'Audio & devices',
    icon: Mic,
    description: 'Microphone, devices, mic-test',
    category: 'preferences',
  },
  {
    id: 'in-browser-ai',
    title: 'In-browser AI',
    icon: Brain,
    description: 'WebGPU model + privacy-mode fallback',
    category: 'preferences',
  },
  {
    id: 'theme',
    title: 'Theme',
    icon: Palette,
    description: 'Appearance (System / Dark)',
    category: 'preferences',
  },
  {
    id: 'billing',
    title: 'Plan & billing',
    icon: CreditCard,
    description: 'Your subscription, upgrade to Pro, invoices',
    category: 'preferences',
  },
  {
    id: 'notifications',
    title: 'Notifications',
    icon: Bell,
    description: 'Meeting alerts and summary delivery',
    category: 'preferences',
  },
  {
    id: 'hotkeys',
    title: 'Hotkeys',
    icon: Keyboard,
    description: 'Keyboard shortcuts for recording',
    category: 'preferences',
  },
  {
    id: 'privacy',
    title: 'Privacy',
    icon: ShieldCheck,
    description: 'Product-analytics opt-out',
    category: 'preferences',
  },
  {
    id: 'invite-codes',
    title: 'Invite codes',
    icon: Ticket,
    description: 'Beta invite codes you can share',
    category: 'preferences',
  },

  // ── Recording defaults ───────────────────────────────────────────
  {
    id: 'recording-defaults',
    title: 'Recording defaults',
    icon: Settings2,
    description: 'Default meeting type, mic+system, storage policy',
    category: 'recording',
  },
  {
    id: 'vocabulary',
    title: 'Custom vocabulary',
    icon: BookOpen,
    description: 'Industry terms and acronyms for transcription',
    category: 'recording',
    adminOnly: true,
  },

  // ── Workspace settings ───────────────────────────────────────────
  {
    id: 'integrations',
    title: 'Integrations',
    icon: Plug,
    description: 'Project-Ops, Contact-Ops, and other Unicorn Commander suite apps',
    category: 'workspace',
    adminOnly: true,
  },
  {
    id: 'report-branding',
    title: 'Report branding',
    icon: FileBadge,
    description: 'Logo, accent, white-label, or unbranded reports',
    category: 'workspace',
    adminOnly: true,
  },
  {
    id: 'calendar-sync',
    title: 'Calendar sync',
    icon: Bell,
    description: 'Auto-create sessions from your calendar',
    category: 'workspace',
    adminOnly: true,
  },
  {
    id: 'sharing-retention',
    title: 'Sharing & retention',
    icon: Share2,
    description: 'Default sharing, retention windows, export',
    category: 'workspace',
    adminOnly: true,
  },
  {
    id: 'speaker-library',
    title: 'Speaker library',
    icon: Library,
    description: 'Workspace voice enrollments and labels',
    category: 'workspace',
    adminOnly: true,
  },

  // ── Admin & appliance ────────────────────────────────────────────
  {
    id: 'providers',
    title: 'AI providers',
    icon: Network,
    description: 'Per-service AI provider routing and BYOK',
    category: 'admin-appliance',
    adminOnly: true,
  },
  {
    id: 'ai',
    title: 'AI & transcription (legacy)',
    icon: Cpu,
    description: 'Legacy LLM model picker — superseded by AI providers',
    category: 'admin-appliance',
  },
  {
    id: 'network',
    title: 'Network',
    icon: Network,
    description: 'Ethernet, IP, DNS settings',
    category: 'admin-appliance',
  },
  {
    id: 'wifi',
    title: 'WiFi',
    icon: Wifi,
    description: 'Wireless network configuration',
    category: 'admin-appliance',
  },
  {
    id: 'performance',
    title: 'Performance',
    icon: Monitor,
    description: 'Device and processing information',
    category: 'admin-appliance',
  },
  {
    id: 'personal-access-tokens',
    title: 'Personal access tokens',
    icon: KeyRound,
    description: 'Per-user MCP tokens (admin policy: future)',
    category: 'admin-appliance',
  },
  {
    id: 'audit-export',
    title: 'Audit & export',
    icon: Download,
    description: 'Audit log download and bulk data export',
    category: 'admin-appliance',
    adminOnly: true,
  },
];

// Note: 'audio' was historically appliance-only because the panel
// configured PipeWire devices on the box. As of the browser-device-picker
// feature, the panel ALSO handles in-browser mic selection (which the
// cloud build needs), so we let it render in cloud mode. The PipeWire
// sub-section inside the panel guards itself on `hostCaps.local_audio`.
export const APPLIANCE_ONLY_SECTIONS = new Set([
  'ai',
  'network',
  'wifi',
  'performance',
]);


export interface HostCapabilities {
  local_audio: boolean;
  npu: boolean;
  build: 'appliance' | 'cloud' | string;
}

export type SettingsState = Record<string, any>;

export const SETTINGS_STORAGE_KEY = 'meeting-ops-settings';

export const DEFAULT_SETTINGS: SettingsState = {
  defaultDevice: 'hw:0,0',
  sampleRate: '44100',
  recordingFormat: 'wav',
  transcriptionModel: 'large-v3',
  liveTranscription: true,
  vadEnabled: true,
  vadThreshold: 0.5,
  chunkDuration: 10,

  enableAI: true,
  summaryFormat: 'executive',
  autoGenerateActions: true,
  speakerDiarization: true,
  maxSpeakers: 4,
  llmModel: 'qwen-3.6-35b-a3b-vision',
  llmProvider: 'llamacpp',

  defaultExportFormat: 'txt',
  includeTimestamps: true,
  includeSpeakerLabels: true,
  autoExport: false,
  exportPath: '/srv/meeting-ops/exports',

  hostname: 'meeting-ops',
  ipMode: 'dhcp',
  ipAddress: '',
  netmask: '255.255.255.0',
  gateway: '',
  dns1: '1.1.1.1',
  dns2: '8.8.8.8',
  vlan: '',

  wifiEnabled: false,
  wifiSSID: '',
  wifiPassword: '',
  wifiSecurity: 'WPA2',

  autoStart: true,
  startOnBoot: true,
  autoUpdate: false,
  updateChannel: 'stable',
  backupEnabled: false,
  backupPath: '/backup',
  backupSchedule: 'daily',

  telemetryEnabled: false,
  logLevel: 'info',
  logRetention: 30,
};

export function loadStoredSettings(): SettingsState {
  if (typeof window === 'undefined') return DEFAULT_SETTINGS;
  const saved = window.localStorage.getItem(SETTINGS_STORAGE_KEY);
  if (saved) {
    try {
      const parsed = JSON.parse(saved);
      // Migrate legacy ms-scaled vadThreshold (>1) to backend 0.0-1.0 unit.
      if (typeof parsed.vadThreshold === 'number' && parsed.vadThreshold > 1) {
        parsed.vadThreshold = parsed.vadThreshold / 1000;
      }
      return parsed;
    } catch (e) {
      console.error('Failed to parse saved settings:', e);
    }
  }
  return DEFAULT_SETTINGS;
}

export interface SettingsContextValue {
  settings: SettingsState;
  setSettings: React.Dispatch<React.SetStateAction<SettingsState>>;
  micVolume: number;
  setMicVolume: React.Dispatch<React.SetStateAction<number>>;
  hostCaps: HostCapabilities;
  onSavedNotice?: (msg: string) => void;
}

const SettingsContext = createContext<SettingsContextValue | null>(null);

export function SettingsProvider({
  value,
  children,
}: {
  value: SettingsContextValue;
  children: React.ReactNode;
}) {
  return (
    <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>
  );
}

export function useSettingsContext(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) {
    throw new Error('useSettingsContext must be used inside SettingsProvider');
  }
  return ctx;
}

export interface SectionProps {
  onSavedNotice?: (msg: string) => void;
}

// IDs of sections that are placeholder shells — no Save button, no state.
// SettingsEnhanced reads this to suppress the Save button row.
export const PLACEHOLDER_SECTIONS = new Set<string>([
  'notifications',
  'hotkeys',
  // 'privacy' persists its toggle immediately (localStorage) — no backend
  // Save row needed, so we list it here to suppress the Save button.
  'privacy',
  // 'invite-codes' is display + copy only (no settings to save).
  'invite-codes',
  'recording-defaults',
  'integrations',
  // Persists through its own scoped API/save action.
  'report-branding',
  'calendar-sync',
  'sharing-retention',
  'speaker-library',
  'audit-export',
]);
