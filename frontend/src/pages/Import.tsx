import { useReducer, useCallback, useRef } from 'react';
import { parseFilename } from '../utils/filenameParser';
import ImportFilePickerStage from '../components/ImportFilePickerStage';
import ImportPreviewTable from '../components/ImportPreviewTable';
import ImportConfirmStage from '../components/ImportConfirmStage';
import ImportProgressStage from '../components/ImportProgressStage';
import ImportCompletionStage from '../components/ImportCompletionStage';

export type Stage = 'pick' | 'preview' | 'confirm' | 'progress' | 'completion';

export interface StagedFile {
  file: File;
  parsed: {
    title: string | null;
    meetingDate: string | null;
    meetingTime: string | null;
    confidence: number;
    source: string | null;
  };
  overrides: {
    title: string;
    meetingDate: string;
    meetingTime: string;
    participantHint: string;
  };
  selected: boolean;
}

export interface ImportState {
  stage: Stage;
  jobId: string | null;
  staged: StagedFile[];
  jobStatus: JobStatusResponse | null;
  error: string | null;
  orgSlug: string | null;
}

export interface JobStatusResponse {
  job_id: string;
  status: string;
  total_files: number;
  succeeded: number;
  failed: number;
  skipped: number;
  started_at: string | null;
  finished_at: string | null;
  cancelled_at: string | null;
  created_at: string;
  files: JobStatusFile[];
}

export interface JobStatusFile {
  file_id: string;
  original_filename: string;
  parsed_title: string | null;
  parsed_date: string | null;
  parsed_time: string | null;
  parsed_confidence: number | null;
  status: string;
  session_id: string | null;
  error_message: string | null;
  bytes_total: number | null;
}

type Action =
  | { type: 'SET_STAGE'; stage: Stage }
  | { type: 'SET_JOB_ID'; jobId: string }
  | { type: 'SET_STAGED'; files: StagedFile[] }
  | { type: 'UPDATE_OVERRIDE'; index: number; field: string; value: string }
  | { type: 'TOGGLE_SELECTED'; index: number }
  | { type: 'SELECT_ALL'; selected: boolean }
  | { type: 'BULK_SET_PARTICIPANT'; participant: string; indices: Set<number> }
  | { type: 'BULK_SET_DATE'; date: string; indices: Set<number> }
  | { type: 'DESELECT_LOW_CONFIDENCE'; threshold: number }
  | { type: 'SET_JOB_STATUS'; status: JobStatusResponse }
  | { type: 'SET_ERROR'; error: string | null }
  | { type: 'SET_ORG_SLUG'; slug: string }
  | { type: 'CLEAR_STAGED' };

function stageReducer(state: ImportState, action: Action): ImportState {
  switch (action.type) {
    case 'SET_STAGE':
      return { ...state, stage: action.stage, error: null };
    case 'SET_JOB_ID':
      return { ...state, jobId: action.jobId };
    case 'SET_STAGED':
      return { ...state, staged: action.files };
    case 'UPDATE_OVERRIDE': {
      const staged = [...state.staged];
      staged[action.index] = {
        ...staged[action.index],
        overrides: { ...staged[action.index].overrides, [action.field]: action.value },
      };
      return { ...state, staged };
    }
    case 'TOGGLE_SELECTED': {
      const staged = [...state.staged];
      staged[action.index] = { ...staged[action.index], selected: !staged[action.index].selected };
      return { ...state, staged };
    }
    case 'SELECT_ALL':
      return {
        ...state,
        staged: state.staged.map((f) => ({ ...f, selected: action.selected })),
      };
    case 'BULK_SET_PARTICIPANT': {
      return {
        ...state,
        staged: state.staged.map((f, i) =>
          action.indices.has(i)
            ? { ...f, overrides: { ...f.overrides, participantHint: action.participant } }
            : f,
        ),
      };
    }
    case 'BULK_SET_DATE': {
      return {
        ...state,
        staged: state.staged.map((f, i) =>
          action.indices.has(i)
            ? { ...f, overrides: { ...f.overrides, meetingDate: action.date } }
            : f,
        ),
      };
    }
    case 'DESELECT_LOW_CONFIDENCE':
      return {
        ...state,
        staged: state.staged.map((f) =>
          f.parsed.confidence < action.threshold ? { ...f, selected: false } : f,
        ),
      };
    case 'SET_JOB_STATUS':
      return { ...state, jobStatus: action.status };
    case 'SET_ERROR':
      return { ...state, error: action.error };
    case 'SET_ORG_SLUG':
      return { ...state, orgSlug: action.slug };
    case 'CLEAR_STAGED':
      return { ...state, staged: [] };
    default:
      return state;
  }
}

const initial: ImportState = {
  stage: 'pick',
  jobId: null,
  staged: [],
  jobStatus: null,
  error: null,
  orgSlug: null,
};

export function extractCallWithName(title: string | null): string {
  if (!title) return '';
  const m = title.match(/^Call\s+with\s+(.+?)(?:\s*\(.*?\))?$/i);
  return m ? m[1].trim() : '';
}

export default function Import() {
  const [state, dispatch] = useReducer(stageReducer, initial);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const resetAll = useCallback(() => {
    dispatch({ type: 'SET_STAGE', stage: 'pick' });
    dispatch({ type: 'SET_JOB_ID', jobId: '' });
    dispatch({ type: 'CLEAR_STAGED' });
    dispatch({ type: 'SET_JOB_STATUS', status: null as unknown as JobStatusResponse });
    dispatch({ type: 'SET_ERROR', error: null });
  }, []);

  return (
    <div className="min-h-screen bg-black text-zinc-100">
      <div className="sticky top-0 z-40 border-b border-zinc-800 bg-black/90 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between">
          <h1 className="text-lg font-semibold">Bulk import</h1>
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            {(['pick', 'preview', 'confirm', 'progress', 'completion'] as const).map(
              (s, i) => (
                <span key={s} className="flex items-center gap-1">
                  {i > 0 && <span className="text-zinc-700">/</span>}
                  <span className={state.stage === s ? 'text-fuchsia-400' : ''}>
                    {s}
                  </span>
                </span>
              ),
            )}
          </div>
        </div>
      </div>

      <div className="mx-auto max-w-6xl px-4 py-6">
        {state.error && (
          <div className="mb-4 rounded-lg border border-red-800 bg-red-950/60 px-4 py-3 text-sm text-red-300">
            {state.error}
            <button
              onClick={() => dispatch({ type: 'SET_ERROR', error: null })}
              className="ml-3 text-red-400 hover:text-red-200"
            >
              dismiss
            </button>
          </div>
        )}

        {state.stage === 'pick' && (
          <ImportFilePickerStage
            staged={state.staged}
            onFilesChosen={(files) => dispatch({ type: 'SET_STAGED', files })}
            onNext={() => dispatch({ type: 'SET_STAGE', stage: 'preview' })}
            onJobCreated={(jobId) => dispatch({ type: 'SET_JOB_ID', jobId })}
            fileInputRef={fileInputRef}
            orgSlug={state.orgSlug}
            onOrgSlug={(slug) => dispatch({ type: 'SET_ORG_SLUG', slug })}
          />
        )}

        {state.stage === 'preview' && (
          <ImportPreviewTable
            staged={state.staged}
            onUpdateOverride={(index, field, value) =>
              dispatch({ type: 'UPDATE_OVERRIDE', index, field, value })
            }
            onToggleSelected={(index) => dispatch({ type: 'TOGGLE_SELECTED', index })}
            onSelectAll={(selected) => dispatch({ type: 'SELECT_ALL', selected })}
            onBulkSetParticipant={(participant, indices) =>
              dispatch({ type: 'BULK_SET_PARTICIPANT', participant, indices })
            }
            onBulkSetDate={(date, indices) =>
              dispatch({ type: 'BULK_SET_DATE', date, indices })
            }
            onDeselectLowConfidence={(threshold) =>
              dispatch({ type: 'DESELECT_LOW_CONFIDENCE', threshold })
            }
            onBack={() => dispatch({ type: 'SET_STAGE', stage: 'pick' })}
            onNext={() => dispatch({ type: 'SET_STAGE', stage: 'confirm' })}
          />
        )}

        {state.stage === 'confirm' && (
          <ImportConfirmStage
            staged={state.staged}
            jobId={state.jobId!}
            onBack={() => dispatch({ type: 'SET_STAGE', stage: 'preview' })}
            onStarted={(jobStatus) => {
              dispatch({ type: 'SET_JOB_STATUS', status: jobStatus });
              dispatch({ type: 'SET_STAGE', stage: 'progress' });
            }}
            onError={(err) => dispatch({ type: 'SET_ERROR', error: err })}
            orgSlug={state.orgSlug}
          />
        )}

        {state.stage === 'progress' && (
          <ImportProgressStage
            jobId={state.jobId!}
            jobStatus={state.jobStatus}
            onStatusUpdate={(status) =>
              dispatch({ type: 'SET_JOB_STATUS', status })
            }
            onComplete={() => dispatch({ type: 'SET_STAGE', stage: 'completion' })}
            onCancel={() => dispatch({ type: 'SET_STAGE', stage: 'pick' })}
            onRetryFailed={(fileId) => {
              // The progress stage handles retry via its own fetch
            }}
          />
        )}

        {state.stage === 'completion' && (
          <ImportCompletionStage
            jobStatus={state.jobStatus}
            onRetryAll={() => dispatch({ type: 'SET_STAGE', stage: 'progress' })}
            onNewImport={resetAll}
          />
        )}
      </div>
    </div>
  );
}
