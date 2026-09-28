import React from 'react';
import { CheckCircle, Loader2, X, AlertCircle, RotateCcw, UploadCloud } from 'lucide-react';
import { useUploads } from '../contexts/UploadsContext';
import { formatBytes, type UploadStage } from '../utils/uploads';

const stageLabels: Record<UploadStage, string> = {
  queued: 'Queued',
  uploading: 'Uploading',
  extracting: 'Extracting',
  transcribing: 'Transcribing',
  diarizing: 'Diarizing',
  summarizing: 'Summarizing',
  embedding: 'Embedding',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export const UploadTray: React.FC = () => {
  const { uploads, cancelUpload, dismissUpload, retryUpload, openSession } = useUploads();
  const visible = uploads.filter((item) => item.stage !== 'cancelled').slice(-8);
  if (visible.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-40 w-[min(380px,calc(100vw-2rem))] rounded-lg border border-gray-700 bg-gray-950 shadow-2xl">
      <div className="flex items-center gap-2 border-b border-gray-800 px-4 py-3">
        <UploadCloud className="h-4 w-4 text-cyan-300" />
        <h2 className="text-sm font-semibold text-white">Uploads</h2>
      </div>
      <div className="max-h-96 overflow-y-auto p-3">
        {visible.map((item) => {
          const inFlight = item.stage !== 'done' && item.stage !== 'failed';
          const canRetry = item.stage === 'failed' && !!item.uploadId;
          return (
            <div key={item.localId} className="mb-3 last:mb-0 rounded-md border border-gray-800 bg-gray-900 p-3">
              <div className="flex items-start gap-3">
                <div className="mt-0.5">
                  {item.stage === 'done' ? (
                    <CheckCircle className="h-4 w-4 text-green-400" />
                  ) : item.stage === 'failed' ? (
                    <AlertCircle className="h-4 w-4 text-red-400" />
                  ) : (
                    <Loader2 className="h-4 w-4 animate-spin text-cyan-300" />
                  )}
                </div>
                <button
                  onClick={() => openSession(item)}
                  disabled={!item.sessionId}
                  className="min-w-0 flex-1 text-left disabled:cursor-default"
                >
                  <div className="truncate text-sm font-medium text-white">{item.filename}</div>
                  <div className="mt-1 flex items-center gap-2 text-xs text-gray-400">
                    <span className="rounded-full border border-gray-700 px-2 py-0.5 text-[11px] text-cyan-200">
                      {stageLabels[item.stage]}
                    </span>
                    <span>{formatBytes(item.size)}</span>
                  </div>
                </button>
                <div className="flex items-center gap-1">
                  {canRetry && (
                    <button
                      onClick={() => retryUpload(item)}
                      className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-cyan-300"
                      title="Retry upload"
                    >
                      <RotateCcw className="h-4 w-4" />
                    </button>
                  )}
                  {inFlight ? (
                    <button
                      onClick={() => cancelUpload(item)}
                      className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-white"
                      title="Cancel upload"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  ) : (
                    <button
                      onClick={() => dismissUpload(item)}
                      className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-white"
                      title="Dismiss"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-gray-800">
                <div
                  className={`h-full ${item.stage === 'failed' ? 'bg-red-500' : 'bg-cyan-400'}`}
                  style={{ width: `${Math.max(3, item.progress)}%` }}
                />
              </div>
              {item.error && (
                <p className="mt-2 line-clamp-2 text-xs text-red-300">{item.error}</p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
