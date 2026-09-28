import React, { useState } from 'react';
import { FileAudio, FileVideo, FileImage, FileText, HelpCircle, X } from 'lucide-react';
import { actionLabelForFile, detectFileKind, estimateProcessingLabel } from '../utils/uploads';
import {
  TranscriptionOptionsPanel,
  DEFAULT_TRANSCRIPTION_OPTIONS,
  serializeTranscriptionOptions,
  type TranscriptionOptions,
} from './TranscriptionOptionsPanel';

interface UploadTriageModalProps {
  files: File[];
  onClose: () => void;
  /** Called when the user confirms. transcriptionOptions is the
   *  serialized options dict (or undefined if all fields are defaults). */
  onTranscribe: (transcriptionOptions?: Record<string, any>) => void;
}

function IconForFile({ file }: { file: File }) {
  const kind = detectFileKind(file);
  if (kind === 'audio') return <FileAudio className="h-4 w-4 text-cyan-300" />;
  if (kind === 'video') return <FileVideo className="h-4 w-4 text-cyan-300" />;
  if (kind === 'image') return <FileImage className="h-4 w-4 text-gray-300" />;
  if (kind === 'document') return <FileText className="h-4 w-4 text-gray-300" />;
  return <HelpCircle className="h-4 w-4 text-gray-400" />;
}

export const UploadTriageModal: React.FC<UploadTriageModalProps> = ({ files, onClose, onTranscribe }) => {
  const [options, setOptions] = useState<TranscriptionOptions>(DEFAULT_TRANSCRIPTION_OPTIONS);

  const handleStart = () => {
    const serialized = serializeTranscriptionOptions(options);
    onTranscribe(serialized);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6">
      <div className="w-full max-w-2xl rounded-lg border border-gray-700 bg-gray-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
          <h2 className="text-lg font-semibold text-white">Confirm upload</h2>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="max-h-[65vh] overflow-y-auto p-5 space-y-4">
          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">Files</p>
            <div className="space-y-2">
              {files.map((file) => (
                <div key={`${file.name}-${file.size}`} className="flex items-center gap-3 rounded-md border border-gray-800 bg-gray-900 px-3 py-2">
                  <IconForFile file={file} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-white">{file.name}</div>
                    <div className="text-xs text-gray-400">{actionLabelForFile(file)} · {estimateProcessingLabel(file)}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <TranscriptionOptionsPanel value={options} onChange={setOptions} defaultOpen />
        </div>
        <div className="flex justify-end gap-3 border-t border-gray-800 px-5 py-4">
          <button onClick={onClose} className="rounded-md border border-gray-700 px-4 py-2 text-sm text-gray-300 hover:bg-gray-800">
            Cancel
          </button>
          <button onClick={handleStart} className="rounded-md bg-cyan-600 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-500">
            Start processing
          </button>
        </div>
      </div>
    </div>
  );
};
