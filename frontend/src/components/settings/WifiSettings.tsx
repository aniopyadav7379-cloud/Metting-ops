import { useSettingsContext } from './SettingsContext';
import type { SectionProps } from './SettingsContext';

export default function WifiSettings(_props: SectionProps) {
  const { settings, setSettings } = useSettingsContext();

  return (
    <div className="space-y-6">
      <div>
        <label className="flex items-center gap-3 mb-4">
          <input
            type="checkbox"
            checked={settings.wifiEnabled}
            onChange={(e) =>
              setSettings({ ...settings, wifiEnabled: e.target.checked })
            }
            className="w-4 h-4 bg-zinc-800 border-zinc-600 rounded"
          />
          <span className="text-sm text-zinc-300">Enable WiFi</span>
        </label>

        {settings.wifiEnabled && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-zinc-300 mb-2">
                Network Name (SSID)
              </label>
              <input
                type="text"
                value={settings.wifiSSID}
                onChange={(e) =>
                  setSettings({ ...settings, wifiSSID: e.target.value })
                }
                placeholder="Your WiFi network name"
                className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-200"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-zinc-300 mb-2">
                Password
              </label>
              <input
                type="password"
                value={settings.wifiPassword}
                onChange={(e) =>
                  setSettings({ ...settings, wifiPassword: e.target.value })
                }
                placeholder="WiFi password"
                className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-200"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-zinc-300 mb-2">
                Security Type
              </label>
              <select
                value={settings.wifiSecurity}
                onChange={(e) =>
                  setSettings({ ...settings, wifiSecurity: e.target.value })
                }
                className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-200"
              >
                <option value="WPA2">WPA2 Personal</option>
                <option value="WPA3">WPA3 Personal</option>
                <option value="WPA2-Enterprise">WPA2 Enterprise</option>
                <option value="Open">Open (No Security)</option>
              </select>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
