import React, { Suspense, useMemo } from 'react';
import {
  AlertCircle,
  CheckCircle,
  ChevronRight,
  RefreshCw,
  Save,
  Settings as SettingsIcon,
} from 'lucide-react';

export type SaveStatus = 'idle' | 'success' | 'error' | 'backend-pending';

/**
 * Settings sections are bucketed into one of four groups per the
 * 2026-05-29 Codex audit (UX-A batch):
 *   - 'preferences'      → per-user client-side surfaces. Mic, in-browser
 *                          AI, theme, notifications, hotkeys.
 *   - 'recording'        → defaults applied to new meetings. Recording
 *                          defaults panel, vocabulary.
 *   - 'workspace'        → per-org, admin-only. Integrations, calendar
 *                          sync, sharing/retention, speaker library.
 *   - 'admin-appliance'  → providers, network, wifi, NPU, PATs, audit.
 *
 * Older 'app' / 'admin' / 'system' / 'advanced' values are retained for
 * backwards compatibility and mapped during render so callers we
 * missed still land somewhere sensible.
 */
export type SettingsCategory =
  | 'preferences'
  | 'recording'
  | 'workspace'
  | 'admin-appliance'
  | 'advanced'
  | 'app'
  | 'admin'
  | 'system';

export interface SettingsSectionMeta {
  id: string;
  title: string;
  icon: React.ElementType;
  description: string;
  category: SettingsCategory;
  /**
   * When true, the section is hidden entirely from non-admin users
   * (not shown disabled — actually omitted). Aaron's directive: "If a
   * setting is admin-only but the user isn't admin, hide it entirely
   * (don't show with a disabled gray state — that's noisy)."
   */
  adminOnly?: boolean;
}

interface SettingsLayoutProps {
  sections: SettingsSectionMeta[];
  activeSection: string;
  onSectionChange: (id: string) => void;
  children: React.ReactNode;
  saving: boolean;
  saveStatus: SaveStatus;
  onSave: () => void;
  showSave?: boolean;
}

function LoadingShell() {
  return (
    <div className="flex items-center justify-center py-16 text-zinc-500">
      <RefreshCw className="w-5 h-5 animate-spin mr-2" />
      <span className="text-sm">Loading settings panel...</span>
    </div>
  );
}

function EmptyShell() {
  return (
    <div className="flex items-center justify-center h-64 text-zinc-500">
      <div className="text-center">
        <SettingsIcon className="w-16 h-16 mx-auto mb-4 text-zinc-700" />
        <p>Select a setting category</p>
      </div>
    </div>
  );
}

export default function SettingsLayout({
  sections,
  activeSection,
  onSectionChange,
  children,
  saving,
  saveStatus,
  onSave,
  showSave = true,
}: SettingsLayoutProps) {
  // Group sections by the 4-tier IA. Legacy categories are mapped to
  // keep older callers rendering somewhere sensible:
  //   'app'      → My preferences
  //   'admin'    → Workspace settings
  //   'system'   → Admin & appliance
  //   'advanced' → Admin & appliance
  // Empty groups are filtered out in the render below. Insertion order
  // here is the visual order of the sidebar groups.
  const groupedSections = useMemo(() => {
    const buckets: Record<string, SettingsSectionMeta[]> = {
      'My preferences': [],
      'Recording defaults': [],
      'Workspace settings': [],
      'Admin & appliance': [],
    };
    for (const s of sections) {
      if (s.category === 'preferences' || s.category === 'app') {
        buckets['My preferences'].push(s);
      } else if (s.category === 'recording') {
        buckets['Recording defaults'].push(s);
      } else if (s.category === 'workspace' || s.category === 'admin') {
        buckets['Workspace settings'].push(s);
      } else {
        // 'admin-appliance' + 'advanced' + 'system' + unknown
        buckets['Admin & appliance'].push(s);
      }
    }
    return buckets;
  }, [sections]);

  const active = sections.find((s) => s.id === activeSection);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
      {/* v3.19 (audit §7 a11y). Sidebar buttons act as tabs (activate
          → show panel) but had no ARIA. Added proper `role="tablist"` /
          `role="tab"` / `aria-selected` / `aria-controls` so screen
          readers + AT can announce + navigate the same way they would
          for any other tab interface. */}
      <div className="lg:col-span-1">
        <div
          className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-2"
          role="tablist"
          aria-orientation="vertical"
          aria-label="Settings sections"
        >
          {Object.entries(groupedSections).map(([category, items]) =>
            items.length === 0 ? null : (
              <div key={category} className="mb-4">
                <div className="text-xs font-semibold text-zinc-500 uppercase tracking-wider px-3 py-2">
                  {category}
                </div>
                {items.map((section) => {
                  const Icon = section.icon;
                  const isActive = activeSection === section.id;
                  return (
                    <button
                      key={section.id}
                      id={`settings-tab-${section.id}`}
                      role="tab"
                      type="button"
                      aria-selected={isActive}
                      aria-controls={`settings-panel-${section.id}`}
                      tabIndex={isActive ? 0 : -1}
                      onClick={() => onSectionChange(section.id)}
                      className={`w-full flex items-center gap-3 px-3 py-3 rounded-xl text-left transition-colors focus:outline-none focus:ring-2 focus:ring-fuchsia-500 ${
                        isActive
                          ? 'bg-zinc-800 text-white'
                          : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'
                      }`}
                    >
                      <Icon className="w-5 h-5 shrink-0" />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium">{section.title}</div>
                        <div className="text-xs text-zinc-500 truncate">
                          {section.description}
                        </div>
                      </div>
                      <ChevronRight className="w-4 h-4 shrink-0" />
                    </button>
                  );
                })}
              </div>
            )
          )}
        </div>
      </div>

      <div className="lg:col-span-3">
        <div
          className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6"
          role="tabpanel"
          id={active ? `settings-panel-${active.id}` : undefined}
          aria-labelledby={active ? `settings-tab-${active.id}` : undefined}
          tabIndex={0}
        >
          <h2 className="text-xl font-semibold text-white mb-6">
            {active?.title}
          </h2>

          <Suspense fallback={<LoadingShell />}>
            {active ? children : <EmptyShell />}
          </Suspense>

          {showSave && (
          <div className="mt-8 pt-6 border-t border-zinc-800">
            <div className="flex items-center gap-3">
              <button
                onClick={onSave}
                disabled={saving}
                className="px-6 py-3 bg-gradient-to-r from-fuchsia-600 to-indigo-600 hover:from-fuchsia-500 hover:to-indigo-500 text-white rounded-xl font-medium flex items-center gap-2 transition-colors disabled:opacity-50"
              >
                {saving ? (
                  <>
                    <RefreshCw className="w-5 h-5 animate-spin" />
                    Saving...
                  </>
                ) : (
                  <>
                    <Save className="w-5 h-5" />
                    Save Changes
                  </>
                )}
              </button>

              {saveStatus === 'success' && (
                <div className="flex items-center gap-2 text-green-400">
                  <CheckCircle className="w-5 h-5" />
                  <span className="text-sm">Settings saved successfully</span>
                </div>
              )}

              {saveStatus === 'error' && (
                <div className="flex items-center gap-2 text-red-400">
                  <AlertCircle className="w-5 h-5" />
                  <span className="text-sm">Failed to save settings</span>
                </div>
              )}

              {saveStatus === 'backend-pending' && (
                <div className="flex items-center gap-2 text-yellow-400">
                  <AlertCircle className="w-5 h-5" />
                  <span className="text-sm">
                    Settings saved locally (backend integration pending)
                  </span>
                </div>
              )}
            </div>
          </div>
          )}
        </div>
      </div>
    </div>
  );
}
