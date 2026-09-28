import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Loader2,
  AlertCircle,
  Mic,
  Network,
  Cpu,
  Users,
  Clock,
  Wand2,
} from 'lucide-react';
import { useOrg } from '../contexts/OrgContext';
import roomsApi, {
  type AudioDevice,
  type RoomSourceType,
} from '../services/roomsApi';
import AudioDeviceList from '../components/rooms/AudioDeviceList';

type WizardStep = 1 | 2 | 3 | 4 | 5;

interface SourceSelection {
  hardware_type: RoomSourceType;
  device?: AudioDevice | null;
  label?: string;
}

interface WizardState {
  name: string;
  location: string;
  source: SourceSelection | null;
  // Step 3: access
  visibility: 'org_members' | 'restricted';
  // Step 4: schedule (Phase 1 = manual)
  recordingMode: 'manual' | 'always_on' | 'scheduled';
  // Step 5: retention
  retentionDays: number | null; // null = unlimited
}

const RETENTION_PRESETS: Array<{ label: string; days: number | null }> = [
  { label: '30 days', days: 30 },
  { label: '60 days', days: 60 },
  { label: '90 days', days: 90 },
  { label: '180 days', days: 180 },
  { label: '1 year', days: 365 },
  { label: 'Unlimited', days: null },
];

const STEP_TITLES: Record<WizardStep, string> = {
  1: 'Room basics',
  2: 'Audio source',
  3: 'Access & retention',
  4: 'Schedule',
  5: 'Review & create',
};

// Feature flag: network_stream has no transport on the backend yet, so the
// tile stays hidden instead of shipping a permanently disabled "Coming soon"
// option. Flip to true when the chunk-based capture transport ships.
const SHOW_NETWORK_STREAM_SOURCE: boolean = false;

export default function RoomSetupWizard() {
  const navigate = useNavigate();
  const { activeOrganization } = useOrg();
  const orgSlug = activeOrganization?.slug || null;

  const [step, setStep] = useState<WizardStep>(1);
  const [state, setState] = useState<WizardState>({
    name: '',
    location: '',
    source: null,
    visibility: 'org_members',
    recordingMode: 'manual',
    retentionDays: 90,
  });
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canAdvance = (() => {
    switch (step) {
      case 1:
        return state.name.trim().length > 0;
      case 2:
        // For server_usb_mic the user must pick a device; the satellite
        // path is configured post-create on the room's pairing screen, so
        // no device is required at this step. network_stream is still
        // disabled (placeholder UI, no transport yet).
        if (!state.source) return false;
        if (state.source.hardware_type === 'server_usb_mic') {
          return state.source.device !== null;
        }
        if (state.source.hardware_type === 'satellite_device') {
          return true;
        }
        return state.source.device !== null;
      case 3:
        return true;
      case 4:
        // All three modes are now permitted (always_on / scheduled persist
        // through schedule_json and are configurable from Room Settings).
        return true;
      case 5:
        return !creating;
      default:
        return false;
    }
  })();

  const handleSubmit = async () => {
    if (!orgSlug) return;
    setCreating(true);
    setError(null);
    try {
      // Persist visibility + recording mode into the room's schedule_json
      // so the user's choices in the wizard actually round-trip. The
      // backend stores it as JSONB on conference_rooms.schedule_json and
      // the rooms-api `normalizeRoom()` already reads `mode` back out as
      // recording_mode. Visibility is read from `schedule_json.access`
      // by RoomDetail's Settings tab.
      const scheduleJson: Record<string, unknown> = {
        mode: state.recordingMode,
        access: state.visibility,
      };

      const room = await roomsApi.create(
        {
          name: state.name.trim(),
          location: state.location.trim() || null,
          default_retention_days: state.retentionDays,
          schedule_json: scheduleJson,
        },
        { orgSlug },
      );
      // Attach the chosen audio source. Use raw_id to support backends
      // that issue UUID room ids.
      const navId = room.raw_id || room.id;
      if (state.source && state.source.device) {
        await roomsApi.addSource(
          navId,
          {
            hardware_type: state.source.hardware_type,
            device_path: state.source.device.device_path,
            label:
              state.source.label?.trim() ||
              state.source.device.device_name ||
              state.source.device.card_name ||
              state.source.device.device_path,
          },
          { orgSlug },
        );
      }
      // Satellite path: jump straight into the room detail's pairing
      // tab. The pairing UI already exists at RoomDetail (#satellite-pair
      // anchor) — we don't duplicate it here, we just deep-link.
      if (state.source && state.source.hardware_type === 'satellite_device') {
        navigate(`/rooms/${navId}#satellite-pair`);
      } else {
        navigate(`/rooms/${navId}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create room');
      setCreating(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl px-4 py-6 sm:px-6">
      <div className="mb-4 flex items-center gap-3">
        <button
          type="button"
          onClick={() => navigate('/rooms')}
          className="rounded-lg border border-zinc-700 bg-zinc-900 p-2 text-zinc-300 hover:bg-zinc-800"
          aria-label="Back to rooms"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Set up a conference room</h1>
          <p className="text-xs text-zinc-500">Step {step} of 5 · {STEP_TITLES[step]}</p>
        </div>
      </div>

      {/* Stepper */}
      <div className="mb-6 flex gap-2">
        {[1, 2, 3, 4, 5].map((n) => (
          <div
            key={n}
            className={`h-1 flex-1 rounded-full ${
              n <= step
                ? 'bg-gradient-to-r from-fuchsia-500 to-indigo-500'
                : 'bg-zinc-800'
            }`}
          />
        ))}
      </div>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
      )}

      <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        {step === 1 && <Step1 state={state} setState={setState} />}
        {step === 2 && (
          <Step2
            state={state}
            setState={setState}
            orgSlug={orgSlug}
          />
        )}
        {step === 3 && <Step3 state={state} setState={setState} />}
        {step === 4 && <Step4 state={state} setState={setState} />}
        {step === 5 && <Step5 state={state} />}
      </div>

      <div className="mt-4 flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={() => {
            if (step === 1) navigate('/rooms');
            else setStep((s) => (s - 1) as WizardStep);
          }}
          disabled={creating}
          className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
        >
          <ArrowLeft className="h-4 w-4" />
          {step === 1 ? 'Cancel' : 'Back'}
        </button>
        {step < 5 ? (
          <button
            type="button"
            onClick={() => setStep((s) => (s + 1) as WizardStep)}
            disabled={!canAdvance}
            className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
          >
            Next
            <ArrowRight className="h-4 w-4" />
          </button>
        ) : (
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canAdvance}
            className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
          >
            {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {creating ? 'Creating…' : 'Create room'}
          </button>
        )}
      </div>
    </div>
  );
}

// -- Step 1 -----------------------------------------------------------------

function Step1({
  state,
  setState,
}: {
  state: WizardState;
  setState: React.Dispatch<React.SetStateAction<WizardState>>;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <label className="block text-xs uppercase tracking-wide text-zinc-500" htmlFor="room-name">
          Room name
        </label>
        <input
          id="room-name"
          type="text"
          value={state.name}
          onChange={(e) => setState((s) => ({ ...s, name: e.target.value }))}
          placeholder="Conference Room A"
          autoFocus
          className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-500"
        />
      </div>
      <div>
        <label className="block text-xs uppercase tracking-wide text-zinc-500" htmlFor="room-location">
          Location <span className="text-zinc-600 normal-case">(optional)</span>
        </label>
        <input
          id="room-location"
          type="text"
          value={state.location}
          onChange={(e) => setState((s) => ({ ...s, location: e.target.value }))}
          placeholder="HQ, 2nd floor"
          className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-500"
        />
      </div>
      <p className="text-xs text-zinc-500">
        Pick a name people will recognize — it'll show in the sidebar and on every recording.
      </p>
    </div>
  );
}

// -- Step 2 -----------------------------------------------------------------

function Step2({
  state,
  setState,
  orgSlug,
}: {
  state: WizardState;
  setState: React.Dispatch<React.SetStateAction<WizardState>>;
  orgSlug: string | null;
}) {
  const selectedType: RoomSourceType | null = state.source?.hardware_type || null;

  const pick = (hardware_type: RoomSourceType) => {
    // network_stream still has no transport on the backend — keep it
    // disabled. server_usb_mic + satellite_device are real paths now.
    if (hardware_type === 'network_stream') return;
    if (hardware_type === 'satellite_device') {
      setState((s) => ({
        ...s,
        source: {
          hardware_type: 'satellite_device',
          device: null, // device gets bound later via pairing on RoomDetail
          label: s.source?.label,
        },
      }));
      return;
    }
    setState((s) => ({
      ...s,
      source: {
        hardware_type: 'server_usb_mic',
        device: s.source?.device ?? null,
        label: s.source?.label,
      },
    }));
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-3">
        <SourceOption
          icon={<Mic className="h-5 w-5" />}
          title="Server-attached USB mic"
          description="The microphone is plugged into the server. Simplest path, recommended for Phase 1 deployments."
          selected={selectedType === 'server_usb_mic'}
          recommended
          onClick={() => pick('server_usb_mic')}
        />
        {SHOW_NETWORK_STREAM_SOURCE && (
          <SourceOption
            icon={<Network className="h-5 w-5" />}
            title="Network audio stream"
            description="Chunk-based capture from a remote PC or audio interface in the room."
            selected={false}
            badge="Coming soon"
            disabled
          />
        )}
        <SourceOption
          icon={<Cpu className="h-5 w-5" />}
          title="Satellite device (RPi / ESP32)"
          description="Dedicated room hardware that pairs over the network. You'll generate a pairing code on the room's detail page after creation."
          selected={selectedType === 'satellite_device'}
          onClick={() => pick('satellite_device')}
        />
      </div>

      {selectedType === 'satellite_device' && (
        <div className="rounded-xl border border-zinc-800 bg-black/30 p-4 text-xs text-zinc-400">
          <p className="font-medium text-zinc-200">Pairing happens after create.</p>
          <p className="mt-1">
            We'll take you to this room's <span className="text-zinc-200">Pair satellite</span>{' '}
            screen after the room is saved. Generate a 6-digit code there, then enter it on the
            satellite device to bind it.
          </p>
        </div>
      )}

      {selectedType === 'server_usb_mic' && (
        <div className="rounded-xl border border-zinc-800 bg-black/30 p-4">
          <AudioDeviceList
            orgSlug={orgSlug}
            selectedDevicePath={state.source?.device?.device_path}
            onSelect={(device) =>
              setState((s) => ({
                ...s,
                source: {
                  hardware_type: 'server_usb_mic',
                  device,
                  label:
                    s.source?.label ||
                    device.device_name ||
                    device.card_name ||
                    device.device_path,
                },
              }))
            }
          />
          {state.source?.device && (
            <div className="mt-4">
              <label
                className="block text-xs uppercase tracking-wide text-zinc-500"
                htmlFor="source-label"
              >
                Label <span className="text-zinc-600 normal-case">(optional)</span>
              </label>
              <input
                id="source-label"
                type="text"
                value={state.source.label || ''}
                onChange={(e) =>
                  setState((s) => ({
                    ...s,
                    source: s.source ? { ...s.source, label: e.target.value } : s.source,
                  }))
                }
                placeholder={
                  state.source.device.device_name ||
                  state.source.device.card_name ||
                  state.source.device.device_path
                }
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-500"
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SourceOption({
  icon,
  title,
  description,
  selected,
  onClick,
  disabled,
  badge,
  recommended,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  selected: boolean;
  onClick?: () => void;
  disabled?: boolean;
  badge?: string;
  recommended?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      className={`flex items-start gap-3 rounded-xl border px-4 py-3 text-left transition-colors ${
        selected
          ? 'border-fuchsia-500/60 bg-fuchsia-500/10'
          : disabled
          ? 'border-zinc-800 bg-zinc-950/40 opacity-60 cursor-not-allowed'
          : 'border-zinc-700 bg-zinc-950 hover:bg-zinc-900'
      }`}
    >
      <div className="mt-0.5 rounded-lg bg-zinc-800 p-2 text-zinc-200">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-zinc-100">{title}</h3>
          {recommended && (
            <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-emerald-200">
              Recommended
            </span>
          )}
          {badge && (
            <span className="rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] uppercase tracking-wide text-zinc-400">
              {badge}
            </span>
          )}
        </div>
        <p className="mt-1 text-xs leading-5 text-zinc-400">{description}</p>
      </div>
    </button>
  );
}

// -- Step 3 -----------------------------------------------------------------

function Step3({
  state,
  setState,
}: {
  state: WizardState;
  setState: React.Dispatch<React.SetStateAction<WizardState>>;
}) {
  return (
    <div className="flex flex-col gap-5">
      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500">Access</div>
        <div className="mt-2 flex flex-col gap-2">
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2">
            <input
              type="radio"
              name="visibility"
              checked={state.visibility === 'org_members'}
              onChange={() => setState((s) => ({ ...s, visibility: 'org_members' }))}
              className="mt-1"
            />
            <div>
              <div className="text-sm text-zinc-100 flex items-center gap-2">
                <Users className="h-3.5 w-3.5" /> Allow all org admins and members
              </div>
              <div className="text-xs text-zinc-500">Default — anyone in the org can view recordings.</div>
            </div>
          </label>
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2">
            <input
              type="radio"
              name="visibility"
              checked={state.visibility === 'restricted'}
              onChange={() => setState((s) => ({ ...s, visibility: 'restricted' }))}
              className="mt-1"
            />
            <div>
              <div className="text-sm text-zinc-100">Custom per-user access</div>
              <div className="text-xs text-zinc-500">
                Only listed users can view recordings. You can manage the list in the room's
                Settings tab after creation.
              </div>
            </div>
          </label>
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-wide text-zinc-500">Retention</div>
        <div className="mt-2 flex flex-wrap gap-2">
          {RETENTION_PRESETS.map((p) => (
            <button
              key={p.label}
              type="button"
              onClick={() => setState((s) => ({ ...s, retentionDays: p.days }))}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium ${
                state.retentionDays === p.days
                  ? 'border-fuchsia-500/60 bg-fuchsia-500/10 text-fuchsia-100'
                  : 'border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          Recordings older than this are deleted automatically. Legal hold pauses retention; you
          can toggle it from the room's Settings tab.
        </p>
      </div>
    </div>
  );
}

// -- Step 4 -----------------------------------------------------------------

function Step4({
  state,
  setState,
}: {
  state: WizardState;
  setState: React.Dispatch<React.SetStateAction<WizardState>>;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="text-xs uppercase tracking-wide text-zinc-500">When should this room record?</div>
      <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2">
        <input
          type="radio"
          name="mode"
          checked={state.recordingMode === 'manual'}
          onChange={() => setState((s) => ({ ...s, recordingMode: 'manual' }))}
          className="mt-1"
        />
        <div>
          <div className="text-sm text-zinc-100 flex items-center gap-2">
            <Wand2 className="h-3.5 w-3.5" /> Manual only
          </div>
          <div className="text-xs text-zinc-500">
            Default for Phase 1. Start/stop the recording from this room's detail page.
          </div>
        </div>
      </label>
      <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2">
        <input
          type="radio"
          name="mode"
          checked={state.recordingMode === 'always_on'}
          onChange={() => setState((s) => ({ ...s, recordingMode: 'always_on' }))}
          className="mt-1"
        />
        <div>
          <div className="text-sm text-zinc-100 flex items-center gap-2">
            <Clock className="h-3.5 w-3.5" /> Always recording
          </div>
          <div className="text-xs text-zinc-500">
            Continuous capture with silence-gap segmentation. The room records whenever its mic
            picks up speech.
          </div>
        </div>
      </label>
      <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2">
        <input
          type="radio"
          name="mode"
          checked={state.recordingMode === 'scheduled'}
          onChange={() => setState((s) => ({ ...s, recordingMode: 'scheduled' }))}
          className="mt-1"
        />
        <div>
          <div className="text-sm text-zinc-100 flex items-center gap-2">
            <Clock className="h-3.5 w-3.5" /> Scheduled
          </div>
          <div className="text-xs text-zinc-500">
            Cron-style schedules. Configure the windows from this room's Settings tab after
            creation.
          </div>
        </div>
      </label>
    </div>
  );
}

// -- Step 5 -----------------------------------------------------------------

function Step5({ state }: { state: WizardState }) {
  const retentionLabel = state.retentionDays
    ? `${state.retentionDays} days`
    : 'Unlimited';
  const modeLabel =
    state.recordingMode === 'always_on'
      ? 'Always recording'
      : state.recordingMode === 'scheduled'
      ? 'Scheduled'
      : 'Manual';
  const sourceLabel = (() => {
    if (!state.source) return '—';
    if (state.source.hardware_type === 'satellite_device') {
      return 'Satellite device · pair after create';
    }
    if (state.source.device) {
      const dev = state.source.device;
      return `Server USB mic · ${state.source.label || dev.device_name || dev.card_name || dev.device_path}`;
    }
    return state.source.hardware_type;
  })();
  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold text-zinc-100">Review</h2>
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Name" value={state.name} />
        <Field label="Location" value={state.location || '—'} />
        <Field label="Audio source" value={sourceLabel} />
        <Field
          label="Access"
          value={state.visibility === 'org_members' ? 'All org members' : 'Custom (set after create)'}
        />
        <Field label="Recording mode" value={modeLabel} />
        <Field label="Retention" value={retentionLabel} />
      </dl>
      <p className="mt-2 text-xs text-zinc-500">
        Press <span className="font-semibold text-zinc-300">Create room</span> to save. You'll be
        taken to the room detail page where you can start recording
        {state.source?.hardware_type === 'satellite_device' ? ' or finish pairing the satellite device.' : '.'}
      </p>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500">{label}</div>
      <div className="mt-0.5 truncate text-sm text-zinc-100">{value}</div>
    </div>
  );
}
