import { useEffect, useState } from 'react';
import { Image, Loader2, RotateCcw, Save, Upload } from 'lucide-react';

import { useOrg } from '../../contexts/OrgContext';
import {
  getReportBranding,
  updateReportBranding,
  type ReportBrandMode,
} from '../../services/reportBrandingApi';
import { showToast } from '../Toast';

const MAX_LOGO_BYTES = 512 * 1024;

export default function ReportBrandingPanel() {
  const { activeOrganization } = useOrg();
  const [displayName, setDisplayName] = useState('');
  const [accentColor, setAccentColor] = useState('#7C3AED');
  const [defaultMode, setDefaultMode] =
    useState<ReportBrandMode>('meeting_ops');
  const [logoDataUri, setLogoDataUri] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getReportBranding(activeOrganization?.slug)
      .then((branding) => {
        if (cancelled) return;
        setDisplayName(branding.display_name);
        setAccentColor(branding.accent_color);
        setDefaultMode(branding.default_mode);
        setLogoDataUri(branding.logo_data_uri || null);
      })
      .catch((error) => {
        if (!cancelled) {
          showToast.error(`Could not load report branding: ${error.message}`);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeOrganization?.slug]);

  const chooseLogo = (file?: File) => {
    if (!file) return;
    if (!['image/png', 'image/jpeg'].includes(file.type)) {
      showToast.error('Use a PNG or JPEG logo.');
      return;
    }
    if (file.size > MAX_LOGO_BYTES) {
      showToast.error('Logo must be 512 KB or smaller.');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setLogoDataUri(String(reader.result || ''));
    reader.onerror = () => showToast.error('Could not read that logo file.');
    reader.readAsDataURL(file);
  };

  const save = async () => {
    setSaving(true);
    try {
      const updated = await updateReportBranding(
        {
          display_name:
            displayName.trim() ||
            activeOrganization?.name ||
            'Meeting Intelligence',
          accent_color: accentColor,
          default_mode: defaultMode,
          ...(logoDataUri
            ? { logo_data_uri: logoDataUri }
            : { clear_logo: true }),
        },
        activeOrganization?.slug,
      );
      setDisplayName(updated.display_name);
      setAccentColor(updated.accent_color);
      setDefaultMode(updated.default_mode);
      setLogoDataUri(updated.logo_data_uri || null);
      showToast.success('Report branding saved.');
    } catch (error) {
      showToast.error(`Could not save branding: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-zinc-400">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading report branding…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-white">Report branding</h3>
        <p className="mt-1 text-sm text-zinc-400">
          Choose the default for PDF, Word, Markdown, and emailed attachments.
          Each download can still override this choice.
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {([
          {
            id: 'meeting_ops',
            title: 'Meeting-Ops',
            copy: 'Our mark and purple report lockup.',
          },
          {
            id: 'workspace',
            title: 'White-label',
            copy: 'Your name, logo, and accent. No Meeting-Ops vendor copy.',
          },
          {
            id: 'unbranded',
            title: 'No heading',
            copy: 'A neutral report with no logo or customer/vendor lockup.',
          },
        ] as const).map((option) => (
          <button
            key={option.id}
            type="button"
            onClick={() => setDefaultMode(option.id)}
            className={`rounded-xl border p-4 text-left transition ${
              defaultMode === option.id
                ? 'border-fuchsia-400/60 bg-fuchsia-500/10'
                : 'border-zinc-800 bg-zinc-900/50 hover:border-zinc-700'
            }`}
          >
            <span className="block text-sm font-medium text-white">
              {option.title}
            </span>
            <span className="mt-1 block text-xs text-zinc-500">
              {option.copy}
            </span>
          </button>
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-[1fr_1.1fr]">
        <div className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900/50 p-5">
          <label className="block">
            <span className="text-sm font-medium text-zinc-200">
              White-label name
            </span>
            <input
              value={displayName}
              maxLength={100}
              onChange={(event) => setDisplayName(event.target.value)}
              className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-white focus:border-fuchsia-500 focus:outline-none"
              placeholder={activeOrganization?.name || 'Your organization'}
            />
          </label>

          <label className="block">
            <span className="text-sm font-medium text-zinc-200">
              Accent color
            </span>
            <span className="mt-1.5 flex items-center gap-2">
              <input
                type="color"
                value={accentColor}
                onChange={(event) => setAccentColor(event.target.value.toUpperCase())}
                className="h-10 w-14 cursor-pointer rounded border border-zinc-700 bg-zinc-950 p-1"
              />
              <input
                value={accentColor}
                pattern="^#[0-9A-Fa-f]{6}$"
                maxLength={7}
                onChange={(event) => setAccentColor(event.target.value)}
                className="h-10 flex-1 rounded-lg border border-zinc-700 bg-zinc-950 px-3 font-mono text-sm text-white focus:border-fuchsia-500 focus:outline-none"
              />
            </span>
          </label>

          <div>
            <span className="text-sm font-medium text-zinc-200">
              White-label logo
            </span>
            <div className="mt-1.5 flex flex-wrap gap-2">
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-white/5">
                <Upload className="h-4 w-4" />
                Choose PNG or JPEG
                <input
                  type="file"
                  accept="image/png,image/jpeg"
                  className="sr-only"
                  onChange={(event) => chooseLogo(event.target.files?.[0])}
                />
              </label>
              {logoDataUri && (
                <button
                  type="button"
                  onClick={() => setLogoDataUri(null)}
                  className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-400 hover:bg-white/5 hover:text-white"
                >
                  <RotateCcw className="h-4 w-4" />
                  Remove
                </button>
              )}
            </div>
            <p className="mt-1 text-xs text-zinc-500">
              Transparent PNG works best. Maximum 512 KB.
            </p>
          </div>
        </div>

        <div className="overflow-hidden rounded-xl border border-zinc-700 bg-white text-slate-700 shadow-2xl">
          <div className="p-6">
            {defaultMode === 'unbranded' ? (
              <div className="h-12" aria-label="No report heading" />
            ) : (
              <div className="flex items-center gap-3">
                {defaultMode === 'workspace' && logoDataUri ? (
                  <img
                    src={logoDataUri}
                    alt=""
                    className="h-12 w-12 object-contain"
                  />
                ) : (
                  <div
                    className="flex h-12 w-12 items-center justify-center rounded-lg text-lg font-bold text-white"
                    style={{
                      backgroundColor:
                        defaultMode === 'workspace' ? accentColor : '#7C3AED',
                    }}
                  >
                    {defaultMode === 'workspace'
                      ? (displayName || 'W').charAt(0).toUpperCase()
                      : 'M'}
                  </div>
                )}
                <div>
                  <div className="font-semibold tracking-wide">
                    {defaultMode === 'workspace'
                      ? displayName || activeOrganization?.name
                      : 'MEETING-OPS'}
                  </div>
                  <div
                    className="text-[10px] font-semibold tracking-wide"
                    style={{
                      color:
                        defaultMode === 'workspace' ? accentColor : '#7C3AED',
                    }}
                  >
                    MEETING INTELLIGENCE REPORT
                  </div>
                </div>
              </div>
            )}
            <div
              className="mt-3 h-0.5 w-full"
              style={{
                backgroundColor:
                  defaultMode === 'workspace'
                    ? accentColor
                    : defaultMode === 'unbranded'
                      ? '#374151'
                      : '#7C3AED',
              }}
            />
            <h4 className="mt-5 text-2xl font-semibold text-slate-800">
              Quarterly planning review
            </h4>
            <div className="mt-3 grid grid-cols-3 gap-px overflow-hidden rounded bg-slate-200 text-xs">
              {['Date', 'Duration', 'Transcript'].map((label) => (
                <div key={label} className="bg-slate-50 p-3">
                  <div className="text-slate-500">{label}</div>
                  <div className="mt-1 font-medium text-slate-700">
                    {label === 'Date'
                      ? 'July 24, 2026'
                      : label === 'Duration'
                        ? '48m 12s'
                        : 'Not included'}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-6 flex items-center gap-2">
              <Image
                className="h-4 w-4"
                style={{
                  color:
                    defaultMode === 'workspace' ? accentColor : '#7C3AED',
                }}
              />
              <span className="text-sm font-semibold">Executive Summary</span>
            </div>
            <div className="mt-2 space-y-2">
              <div className="h-2 rounded bg-slate-200" />
              <div className="h-2 w-5/6 rounded bg-slate-200" />
            </div>
          </div>
        </div>
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="inline-flex items-center gap-2 rounded-lg bg-fuchsia-600 px-4 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
        >
          {saving ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          Save report branding
        </button>
      </div>
    </div>
  );
}
