// Desktop tab strip for SessionDetails.
//
// Mirrors the pill-tab pattern used by MobileSessionDetails (a horizontal
// strip of buttons styled as pills) so the desktop and mobile IA are
// visually + structurally consistent. Radix Tabs isn't in deps and adding
// it just for one strip would be overkill — this is the same lightweight
// controlled-button pattern, with a sticky container so the tabs stay
// visible while the user scrolls the active panel.
//
// Audit context: SessionDetails.tsx was a single ~3700 line scroll where
// summary, transcript, action items, speakers, attachments and per-meeting
// AI chat were all stacked vertically. This component is half of the IA
// fix; the page itself wraps each section in `tab === '...' && (...)`.

import { FileText, Sparkles, ListChecks, Users, Paperclip, MessageCircle } from 'lucide-react';

export type SessionDetailsTab =
  | 'summary'
  | 'transcript'
  | 'action_items'
  | 'speakers'
  | 'attachments'
  | 'chat';

export const SESSION_DETAILS_TABS: Array<{
  key: SessionDetailsTab;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  { key: 'summary', label: 'Summary', icon: Sparkles },
  { key: 'transcript', label: 'Transcript', icon: FileText },
  { key: 'action_items', label: 'Action items', icon: ListChecks },
  { key: 'speakers', label: 'Speakers', icon: Users },
  { key: 'attachments', label: 'Attachments', icon: Paperclip },
  { key: 'chat', label: 'Chat', icon: MessageCircle },
];

interface Props {
  value: SessionDetailsTab;
  onChange: (next: SessionDetailsTab) => void;
  /** When true, the Chat tab is rendered disabled with a tooltip. */
  chatDisabled?: boolean;
  chatDisabledReason?: string;
}

export default function SessionDetailsTabs({
  value,
  onChange,
  chatDisabled,
  chatDisabledReason,
}: Props) {
  return (
    <div
      role="tablist"
      aria-label="Session sections"
      className="flex items-center gap-1.5 overflow-x-auto px-1 py-1"
    >
      {SESSION_DETAILS_TABS.map((tab) => {
        const Icon = tab.icon;
        const active = value === tab.key;
        const disabled = tab.key === 'chat' && chatDisabled;
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={active}
            aria-controls={`session-tab-panel-${tab.key}`}
            id={`session-tab-${tab.key}`}
            disabled={disabled}
            title={disabled ? chatDisabledReason : undefined}
            onClick={() => !disabled && onChange(tab.key)}
            className={
              'flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors ' +
              (active
                ? 'bg-gradient-to-r from-purple-600 to-indigo-600 text-white shadow-sm'
                : disabled
                ? 'cursor-not-allowed border border-gray-200 bg-gray-50 text-gray-400'
                : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50')
            }
          >
            <Icon className="h-3.5 w-3.5" />
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
