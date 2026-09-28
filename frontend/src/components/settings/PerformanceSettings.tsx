import { Cpu } from 'lucide-react';
import type { SectionProps } from './SettingsContext';

// The NPU-acceleration banner, NPU toggle/power-mode, CPU-threads, memory-limit,
// and "Reference Hardware" blocks were removed (pre-launch cleanup): none were
// wired to a backend endpoint, and the "AMD Phoenix NPU / 220x" + "0.0045 RTF"
// copy did not describe the actual stack. Server STT/diarization/LLM run on the
// GPU cluster (Parakeet 1.1B, pyannote 3.1, Qwen 3.6 35B-A3B-Vision) and are not
// user-tunable here. This panel is intentionally a no-op stub until a real,
// backend-wired performance control exists.
export default function PerformanceSettings(_props: SectionProps) {
  return (
    <div className="space-y-6">
      <div className="bg-zinc-800/50 border border-zinc-700 rounded-lg p-4">
        <div className="flex items-start gap-3">
          <Cpu className="w-5 h-5 text-zinc-400 mt-0.5" />
          <div>
            <p className="text-sm text-zinc-200 font-medium">
              Nothing to configure here
            </p>
            <p className="text-xs text-zinc-400 mt-1">
              Live transcription runs in your browser; the completion pass
              (Parakeet 1.1B, pyannote 3.1, Qwen 3.6) runs on the server. There
              are no device-level performance settings to tune from this screen.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
