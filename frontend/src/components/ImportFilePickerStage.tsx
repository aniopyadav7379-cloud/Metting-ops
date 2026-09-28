import { useCallback, useState, useRef, type RefObject } from 'react';
import { showToast } from './Toast';
import { parseFilename } from '../utils/filenameParser';
import { FolderUp, FileWarning, Check, Sparkles } from 'lucide-react';
import type { StagedFile } from '../pages/Import';
import { useTierFeatures } from '../hooks/useTierFeatures';

const MAX_FILES = 1000;
const AUDIO_MIME_TYPES = new Set([
  'audio/mpeg', 'audio/mp4', 'audio/m4a', 'audio/wav', 'audio/wave',
  'audio/x-wav', 'audio/flac', 'audio/ogg', 'audio/opus', 'audio/aac',
  'audio/x-m4a', 'audio/webm', 'video/mp4', 'video/webm', 'video/mpeg',
  'video/x-matroska', 'video/quicktime',
]);
const AUDIO_EXTENSIONS = new Set([
  '.m4a', '.mp3', '.wav', '.flac', '.ogg', '.opus', '.aac',
  '.mp4', '.mov', '.mkv', '.webm', '.avi',
]);

function isAudioFile(file: File): boolean {
  if (AUDIO_MIME_TYPES.has(file.type)) return true;
  const ext = '.' + file.name.split('.').pop()?.toLowerCase();
  return AUDIO_EXTENSIONS.has(ext);
}

interface Props {
  staged: StagedFile[];
  onFilesChosen: (files: StagedFile[]) => void;
  onNext: () => void;
  onJobCreated: (jobId: string) => void;
  fileInputRef: RefObject<HTMLInputElement | null>;
  orgSlug: string | null;
  onOrgSlug: (slug: string) => void;
}

export default function ImportFilePickerStage({
  staged, onFilesChosen, onNext, onJobCreated, fileInputRef, orgSlug, onOrgSlug,
}: Props) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const dropRef = useRef<HTMLDivElement>(null);
  // v3.19 (audit §5). Bulk import = Pro/Enterprise — it spins up the
  // server completion pipeline on every file, which is exactly the
  // compute path free tier doesn't get. We gate at the job-creation
  // entry so a free user can't even reach the file picker. Backend
  // mirrors this with a 403 in `auth/tier.py`.
  const { hasFeature } = useTierFeatures();
  const canBulkImport = hasFeature('bulk_import');

  const processFiles = useCallback(
    (fileList: FileList | File[]) => {
      const files = Array.from(fileList);
      const valid: StagedFile[] = [];
      for (const f of files) {
        if (!isAudioFile(f)) {
          showToast.warning(`Skipped ${f.name}: not an audio file`);
          continue;
        }
        const parsed = parseFilename(f.name);
        valid.push({
          file: f,
          parsed: {
            title: parsed.title,
            meetingDate: parsed.meetingDate,
            meetingTime: parsed.meetingTime,
            confidence: parsed.confidence,
            source: parsed.source,
          },
          overrides: {
            title: parsed.title ?? '',
            meetingDate: parsed.meetingDate ?? '',
            meetingTime: parsed.meetingTime ?? '',
            participantHint: '',
          },
          selected: true,
        });
        if (valid.length >= MAX_FILES) {
          showToast.warning(`Max ${MAX_FILES} files per job; extra files ignored`);
          break;
        }
      }
      if (valid.length === 0) {
        showToast.error('No valid audio files found');
        return;
      }
      onFilesChosen(valid);
    },
    [onFilesChosen],
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const items = e.dataTransfer?.items;
      if (!items) {
        if (e.dataTransfer?.files?.length) {
          processFiles(e.dataTransfer.files);
        }
        return;
      }
      // Try webkitGetAsEntry for directory support
      const allFiles: File[] = [];
      const promises: Promise<void>[] = [];
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry?.();
        if (entry) {
          promises.push(traverseEntry(entry, allFiles));
        } else {
          // getAsFile is always defined on DataTransferItem; check the
          // return value, which can be null when the item isn't a File.
          const f = items[i].getAsFile();
          if (f) allFiles.push(f);
        }
      }
      Promise.all(promises).then(() => processFiles(allFiles));
    },
    [processFiles],
  );

  const handleFileInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files?.length) processFiles(e.target.files);
    },
    [processFiles],
  );

  const handleCreateJob = useCallback(async () => {
    if (!canBulkImport) {
      // Belt-and-suspenders: the upgrade card hides the button when this
      // is false, but a stale-state click should still bail cleanly.
      showToast.warning('Bulk import requires Pro. Upgrade from the pricing page to enable it.');
      return;
    }
    setCreating(true);
    try {
      const resp = await fetch('/api/import/jobs', {
        method: 'POST',
        credentials: 'include',
      });
      if (!resp.ok) throw new Error(`create job failed: ${resp.status}`);
      const data = await resp.json();
      setJobId(data.job_id);
      onJobCreated(data.job_id);
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Failed to create job');
    } finally {
      setCreating(false);
    }
  }, [canBulkImport, onJobCreated]);

  const totalSize = staged.reduce((s, f) => s + f.file.size, 0);
  const totalMb = (totalSize / (1024 * 1024)).toFixed(1);

  return (
    <div className="space-y-6">
      {/* Step 1: Create job (or upgrade prompt for free tier). */}
      {!jobId && canBulkImport && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6">
          <h2 className="mb-2 text-base font-medium">Step 1: Create import job</h2>
          <p className="mb-4 text-sm text-zinc-400">
            Each bulk import creates a job that tracks your files through processing.
          </p>
          <button
            onClick={handleCreateJob}
            disabled={creating}
            className="rounded-lg bg-fuchsia-600 px-5 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
          >
            {creating ? 'Creating...' : 'Create import job'}
          </button>
        </div>
      )}
      {!jobId && !canBulkImport && (
        <div className="rounded-lg border border-purple-500/30 bg-purple-500/5 p-6">
          <div className="flex items-start gap-3">
            <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-purple-300" />
            <div className="min-w-0 flex-1">
              <h2 className="text-base font-medium text-purple-100">Bulk import is a Pro workflow</h2>
              <p className="mt-1 text-sm text-purple-200/80">
                Bulk import is a Pro workflow for processing existing
                audio libraries. Free keeps new recordings local in
                this browser. Upgrade to queue files, track progress,
                and run the server completion pipeline.
              </p>
              <a
                href="#/pricing"
                className="mt-4 inline-flex items-center rounded-lg bg-purple-600 px-5 py-2 text-sm font-medium text-white hover:bg-purple-500"
              >
                View Pro pricing
              </a>
            </div>
          </div>
        </div>
      )}

      {/* Step 2: Pick files */}
      {jobId && (
        <>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6">
            <h2 className="mb-2 text-base font-medium">
              Step 2: Choose audio files
            </h2>
            <p className="mb-4 text-sm text-zinc-400">
              Drop a folder or select individual files. Audio files only
              (m4a, mp3, wav, flac, ogg, opus, aac, mp4, mov, mkv, webm).
              Max {MAX_FILES} files per job.
            </p>

            <div
              ref={dropRef}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-10 transition-colors ${
                dragOver
                  ? 'border-fuchsia-500 bg-fuchsia-950/20'
                  : 'border-zinc-700 hover:border-zinc-500'
              }`}
            >
              <FolderUp className="mb-3 h-10 w-10 text-zinc-500" />
              <p className="mb-2 text-sm text-zinc-300">
                Drag and drop a folder or files here
              </p>
              <p className="mb-4 text-xs text-zinc-500">or</p>
              <label className="cursor-pointer rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-700">
                Browse files
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  {...({webkitdirectory: ""} as React.InputHTMLAttributes<HTMLInputElement>)}
                  accept=".m4a,.mp3,.wav,.flac,.ogg,.opus,.aac,.mp4,.mov,.mkv,.webm,.avi,audio/*,video/*"
                  onChange={handleFileInput}
                  className="hidden"
                />
              </label>
              <p className="mt-3 text-xs text-zinc-600">
                Note: webkitdirectory opens a folder picker in Chrome/Edge.
                Drag-drop works in all browsers.
              </p>
            </div>
          </div>

          {/* Selected count */}
          {staged.length > 0 && (
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Check className="h-5 w-5 text-emerald-400" />
                  <span className="text-sm">
                    <strong>{staged.length}</strong> files selected ({totalMb} MB)
                  </span>
                </div>
                <button
                  onClick={onNext}
                  disabled={staged.length === 0}
                  className="rounded-lg bg-fuchsia-600 px-5 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
                >
                  Preview & edit ({staged.length})
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function traverseEntry(
  entry: FileSystemEntry,
  out: File[],
): Promise<void> {
  return new Promise((resolve) => {
    if (entry.isFile) {
      (entry as FileSystemFileEntry).file(
        (file) => {
          out.push(file);
          resolve();
        },
        () => resolve(),
      );
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      reader.readEntries((entries) => {
        Promise.all(entries.map((e) => traverseEntry(e, out))).then(() =>
          resolve(),
        );
      }, () => resolve());
    } else {
      resolve();
    }
  });
}
