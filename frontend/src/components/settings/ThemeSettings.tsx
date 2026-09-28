import { Monitor, Moon } from 'lucide-react';
import { useTheme } from '../../contexts/ThemeContext';
import type { SectionProps } from './SettingsContext';

/**
 * Theme settings panel. Exposes the existing ThemeContext setter as a UI
 * control under Settings → My preferences. Per UX-A scope: two options
 * only (System / Dark). Light is genuinely undertested, so we don't
 * surface it as a third option even though ThemeContext supports it.
 */
export default function ThemeSettings(_props: SectionProps) {
  const { theme, setTheme } = useTheme();

  // Treat 'light' (legacy stored value) as 'system' for the radio state —
  // the user can re-select System or Dark explicitly. We don't actively
  // migrate the stored value; ThemeContext already handles light correctly
  // at the effective-theme layer if it's still there.
  const selected: 'system' | 'dark' = theme === 'dark' ? 'dark' : 'system';

  const options: Array<{
    id: 'system' | 'dark';
    label: string;
    description: string;
    icon: typeof Monitor;
  }> = [
    {
      id: 'system',
      label: 'System',
      description: 'Follow your operating system appearance',
      icon: Monitor,
    },
    {
      id: 'dark',
      label: 'Dark',
      description: 'Dark interface, always',
      icon: Moon,
    },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-medium text-zinc-200 mb-1">Appearance</h3>
        <p className="text-xs text-zinc-500">
          Light mode is intentionally not offered — the UI is tuned for
          dark and System (which still resolves to dark on light OSes today).
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {options.map((opt) => {
          const Icon = opt.icon;
          const active = selected === opt.id;
          return (
            <button
              key={opt.id}
              type="button"
              onClick={() => setTheme(opt.id)}
              className={`flex items-start gap-3 rounded-xl border p-4 text-left transition-colors ${
                active
                  ? 'border-fuchsia-400/50 bg-fuchsia-500/10 text-white'
                  : 'border-zinc-800 bg-zinc-900/40 text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900/70'
              }`}
              aria-pressed={active}
            >
              <Icon
                className={`w-5 h-5 shrink-0 mt-0.5 ${
                  active ? 'text-fuchsia-300' : 'text-zinc-500'
                }`}
              />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{opt.label}</div>
                <div className="text-xs text-zinc-500 mt-0.5">
                  {opt.description}
                </div>
              </div>
              <div
                className={`mt-1 h-4 w-4 shrink-0 rounded-full border ${
                  active
                    ? 'border-fuchsia-300 bg-fuchsia-400'
                    : 'border-zinc-600'
                }`}
                aria-hidden
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}
