import { useEffect, useState } from 'react';
import { config } from '../../config';
import { useSettingsContext } from './SettingsContext';
import type { SectionProps } from './SettingsContext';

export default function NetworkSettings(_props: SectionProps) {
  const { settings, setSettings } = useSettingsContext();
  const [networkInfo, setNetworkInfo] = useState<any>({});

  const fetchNetworkInfo = async () => {
    try {
      const response = await fetch(`${config.apiUrl}/api/simple/system/network`);
      if (response.ok) {
        const info = await response.json();
        setNetworkInfo(info);
        setSettings((prev: any) => ({
          ...prev,
          hostname: info.hostname || prev.hostname,
          ipAddress: info.ipAddress || prev.ipAddress,
          gateway: info.gateway || prev.gateway,
        }));
      }
    } catch (error) {
      console.error('Failed to fetch network info:', error);
    }
  };

  useEffect(() => {
    fetchNetworkInfo();
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <label className="block text-sm font-medium text-zinc-300 mb-2">
          Hostname
        </label>
        <input
          type="text"
          value={settings.hostname}
          onChange={(e) => setSettings({ ...settings, hostname: e.target.value })}
          className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-zinc-200"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-zinc-300 mb-2">
          IP Configuration
        </label>
        <div className="flex gap-2 mb-4">
          <button
            onClick={() => setSettings({ ...settings, ipMode: 'dhcp' })}
            className={`px-4 py-2 rounded-lg text-sm ${
              settings.ipMode === 'dhcp'
                ? 'bg-zinc-700 text-white'
                : 'bg-zinc-800 text-zinc-400 hover:text-white'
            }`}
          >
            DHCP (Automatic)
          </button>
          <button
            onClick={() => setSettings({ ...settings, ipMode: 'static' })}
            className={`px-4 py-2 rounded-lg text-sm ${
              settings.ipMode === 'static'
                ? 'bg-zinc-700 text-white'
                : 'bg-zinc-800 text-zinc-400 hover:text-white'
            }`}
          >
            Static IP
          </button>
        </div>

        {settings.ipMode === 'static' && (
          <div className="space-y-3">
            <div>
              <label className="block text-xs text-zinc-400 mb-1">IP Address</label>
              <input
                type="text"
                value={settings.ipAddress}
                onChange={(e) =>
                  setSettings({ ...settings, ipAddress: e.target.value })
                }
                placeholder="192.168.1.100"
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-200"
              />
            </div>
            <div>
              <label className="block text-xs text-zinc-400 mb-1">
                Subnet Mask
              </label>
              <input
                type="text"
                value={settings.netmask}
                onChange={(e) =>
                  setSettings({ ...settings, netmask: e.target.value })
                }
                placeholder="255.255.255.0"
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-200"
              />
            </div>
            <div>
              <label className="block text-xs text-zinc-400 mb-1">Gateway</label>
              <input
                type="text"
                value={settings.gateway}
                onChange={(e) =>
                  setSettings({ ...settings, gateway: e.target.value })
                }
                placeholder="192.168.1.1"
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-200"
              />
            </div>
          </div>
        )}
      </div>

      <div>
        <label className="block text-sm font-medium text-zinc-300 mb-2">
          DNS Servers
        </label>
        <div className="space-y-2">
          <input
            type="text"
            value={settings.dns1}
            onChange={(e) => setSettings({ ...settings, dns1: e.target.value })}
            placeholder="Primary DNS (e.g., 1.1.1.1)"
            className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-200"
          />
          <input
            type="text"
            value={settings.dns2}
            onChange={(e) => setSettings({ ...settings, dns2: e.target.value })}
            placeholder="Secondary DNS (e.g., 8.8.8.8)"
            className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-200"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-zinc-300 mb-2">
          VLAN ID (Optional)
        </label>
        <input
          type="text"
          value={settings.vlan}
          onChange={(e) => setSettings({ ...settings, vlan: e.target.value })}
          placeholder="Leave empty for no VLAN"
          className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm text-zinc-200"
        />
      </div>

      {networkInfo.ipAddress && (
        <div className="bg-zinc-800/50 rounded-lg p-4 mt-6">
          <h4 className="text-sm font-medium text-zinc-300 mb-3">
            Current Network Status
          </h4>
          <div className="space-y-2 text-xs">
            <div className="flex justify-between">
              <span className="text-zinc-400">Current IP:</span>
              <span className="text-zinc-200 font-mono">
                {networkInfo.ipAddress}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-zinc-400">Gateway:</span>
              <span className="text-zinc-200 font-mono">{networkInfo.gateway}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-zinc-400">Interface:</span>
              <span className="text-zinc-200">
                {networkInfo.interface || 'eth0'}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
