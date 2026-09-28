import React, { useEffect, useState } from 'react';
import { UploadCloud } from 'lucide-react';
import { UploadTriageModal } from './UploadTriageModal';
import { useUploads } from '../contexts/UploadsContext';
import { defaultActionForFile } from "../utils/uploads";

export const DragDropOverlay: React.FC = () => {
  const { startUploads } = useUploads();
  const [dragging, setDragging] = useState(false);
  const [triageFiles, setTriageFiles] = useState<File[] | null>(null);

  useEffect(() => {
    let depth = 0;
    const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer?.types ?? []).includes('Files');

    const onDragEnter = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      depth += 1;
      setDragging(true);
    };
    const onDragOver = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
    };
    const onDragLeave = () => {
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const onDrop = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      depth = 0;
      setDragging(false);
      const files = Array.from(event.dataTransfer?.files ?? []);
      handleFiles(files);
    };

    window.addEventListener('dragenter', onDragEnter);
    window.addEventListener('dragover', onDragOver);
    window.addEventListener('dragleave', onDragLeave);
    window.addEventListener('drop', onDrop);
    return () => {
      window.removeEventListener('dragenter', onDragEnter);
      window.removeEventListener('dragover', onDragOver);
      window.removeEventListener('dragleave', onDragLeave);
      window.removeEventListener('drop', onDrop);
    };
  }, []);

  const handleFiles = (files: File[]) => {
    if (files.length === 0) return;
    // Drag-drop is the fast path: single obvious audio/video file goes
    // straight to processing with the org defaults (Parakeet STT,
    // pyannote auto-detect, balanced sensitivity). Mixed file selections
    // or non-obvious file types fall through to the triage modal where
    // the user picks per-file options. The Re-process button on
    // SessionDetails covers tweaking options after the fact.
    const single = files.length === 1 ? files[0] : null;
    const defaultAction = single ? defaultActionForFile(single) : null;
    if (single && defaultAction === "transcribe") {
      void startUploads(files);
      return;
    }
    setTriageFiles(files);
  };

  return (
    <>
      {dragging && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-8">
          <div className="flex h-full w-full flex-col items-center justify-center rounded-lg border-2 border-dashed border-cyan-400 bg-gray-950/80 text-center">
            <UploadCloud className="mb-4 h-14 w-14 text-cyan-300" />
            <h2 className="text-3xl font-semibold text-white">Drop to upload</h2>
            <p className="mt-2 text-sm text-gray-300">Audio and video become processed meetings</p>
            <p className="mt-6 text-xs text-gray-500">Drop outside to cancel</p>
          </div>
        </div>
      )}
      {triageFiles && (
        <UploadTriageModal
          files={triageFiles}
          onClose={() => setTriageFiles(null)}
          onTranscribe={(transcriptionOptions) => {
            const files = triageFiles;
            setTriageFiles(null);
            void startUploads(files, transcriptionOptions ? { transcriptionOptions } : undefined);
          }}
        />
      )}
    </>
  );
};
