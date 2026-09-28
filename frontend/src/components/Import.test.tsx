import { parseFilename } from "../utils/filenameParser";
import { describe, expect, it } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useReducer } from 'react';

type Stage = 'pick' | 'preview' | 'confirm' | 'progress' | 'completion';

interface StagedFile {
  file: { name: string; size: number };
  parsed: { title: string | null; meetingDate: string | null; meetingTime: string | null; confidence: number; source: string | null };
  overrides: { title: string; meetingDate: string; meetingTime: string; participantHint: string };
  selected: boolean;
}

interface ImportState {
  stage: Stage;
  staged: StagedFile[];
  error: string | null;
}

type Action =
  | { type: 'SET_STAGE'; stage: Stage }
  | { type: 'SET_STAGED'; files: StagedFile[] }
  | { type: 'TOGGLE_SELECTED'; index: number }
  | { type: 'SELECT_ALL'; selected: boolean }
  | { type: 'BULK_SET_PARTICIPANT'; participant: string; indices: Set<number> }
  | { type: 'BULK_SET_DATE'; date: string; indices: Set<number> }
  | { type: 'DESELECT_LOW_CONFIDENCE'; threshold: number }
  | { type: 'SET_ERROR'; error: string | null };

function stageReducer(state: ImportState, action: Action): ImportState {
  switch (action.type) {
    case 'SET_STAGE':
      return { ...state, stage: action.stage, error: null };
    case 'SET_STAGED':
      return { ...state, staged: action.files };
    case 'TOGGLE_SELECTED': {
      const staged = [...state.staged];
      staged[action.index] = { ...staged[action.index], selected: !staged[action.index].selected };
      return { ...state, staged };
    }
    case 'SELECT_ALL':
      return { ...state, staged: state.staged.map((f) => ({ ...f, selected: action.selected })) };
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
      return { ...state, staged: state.staged.map((f) => (f.parsed.confidence < action.threshold ? { ...f, selected: false } : f)) };
    case 'SET_ERROR':
      return { ...state, error: action.error };
    default:
      return state;
  }
}

const mockFile = (name: string, confidence: number): StagedFile => ({
  file: { name, size: 1000 },
  parsed: { title: 'Parsed ' + name, meetingDate: null, meetingTime: null, confidence, source: 'generic' },
  overrides: { title: '', meetingDate: '', meetingTime: '', participantHint: '' },
  selected: true,
});

describe('Import stage transitions', () => {
  it('starts at pick stage', () => {
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'pick', staged: [], error: null }),
    );
    expect(result.current[0].stage).toBe('pick');
  });

  it('advances through stages', () => {
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'pick', staged: [], error: null }),
    );
    const [, dispatch] = result.current;

    act(() => dispatch({ type: 'SET_STAGE', stage: 'preview' }));
    expect(result.current[0].stage).toBe('preview');

    act(() => dispatch({ type: 'SET_STAGE', stage: 'confirm' }));
    expect(result.current[0].stage).toBe('confirm');

    act(() => dispatch({ type: 'SET_STAGE', stage: 'progress' }));
    expect(result.current[0].stage).toBe('progress');

    act(() => dispatch({ type: 'SET_STAGE', stage: 'completion' }));
    expect(result.current[0].stage).toBe('completion');
  });

  it('can go back to pick from any stage', () => {
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'progress', staged: [], error: null }),
    );
    const [, dispatch] = result.current;
    act(() => dispatch({ type: 'SET_STAGE', stage: 'pick' }));
    expect(result.current[0].stage).toBe('pick');
  });

  it('clears error on stage change', () => {
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'confirm', staged: [], error: 'something broke' }),
    );
    const [, dispatch] = result.current;
    expect(result.current[0].error).toBe('something broke');
    act(() => dispatch({ type: 'SET_STAGE', stage: 'progress' }));
    expect(result.current[0].error).toBeNull();
  });
});

describe('Import bulk-edit reducer actions', () => {
  it('bulk sets participant hint on selected rows', () => {
    const files = [mockFile('a.m4a', 0.9), mockFile('b.m4a', 0.5), mockFile('c.m4a', 0.3)];
    files[1].selected = false;
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'preview', staged: files, error: null }),
    );
    const [, dispatch] = result.current;
    act(() => dispatch({ type: 'BULK_SET_PARTICIPANT', participant: 'Jason Allen', indices: new Set([0, 2]) }));
    expect(result.current[0].staged[0].overrides.participantHint).toBe('Jason Allen');
    expect(result.current[0].staged[1].overrides.participantHint).toBe('');
    expect(result.current[0].staged[2].overrides.participantHint).toBe('Jason Allen');
  });

  it('bulk sets date on selected rows', () => {
    const files = [mockFile('a.m4a', 0.9), mockFile('b.m4a', 0.5)];
    files[0].selected = false;
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'preview', staged: files, error: null }),
    );
    const [, dispatch] = result.current;
    act(() => dispatch({ type: 'BULK_SET_DATE', date: '2026-05-22', indices: new Set([1]) }));
    expect(result.current[0].staged[0].overrides.meetingDate).toBe('');
    expect(result.current[0].staged[1].overrides.meetingDate).toBe('2026-05-22');
  });

  it('deselects low confidence rows', () => {
    const files = [mockFile('high.m4a', 0.9), mockFile('med.m4a', 0.6), mockFile('low.m4a', 0.3)];
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'preview', staged: files, error: null }),
    );
    const [, dispatch] = result.current;
    expect(result.current[0].staged.every((f) => f.selected)).toBe(true);
    act(() => dispatch({ type: 'DESELECT_LOW_CONFIDENCE', threshold: 0.5 }));
    expect(result.current[0].staged[0].selected).toBe(true);
    expect(result.current[0].staged[1].selected).toBe(true);
    expect(result.current[0].staged[2].selected).toBe(false);
  });

  it('selects all / deselects all', () => {
    const files = [mockFile('a.m4a', 0.9), mockFile('b.m4a', 0.5)];
    files[0].selected = false;
    const { result } = renderHook(() =>
      useReducer(stageReducer, { stage: 'preview', staged: files, error: null }),
    );
    const [, dispatch] = result.current;
    act(() => dispatch({ type: 'SELECT_ALL', selected: true }));
    expect(result.current[0].staged.every((f) => f.selected)).toBe(true);
    act(() => dispatch({ type: 'SELECT_ALL', selected: false }));
    expect(result.current[0].staged.every((f) => !f.selected)).toBe(true);
  });
});

describe('parseFilename preview pre-fills', () => {
  it('pre-fills title and date from notes pattern', () => {
    const p = parseFilename('notes__2026-05-20_143000__Call with Aaron.m4a');
    expect(p.title).toBe('Call with Aaron');
    expect(p.meetingDate).toBe('2026-05-20');
    expect(p.meetingTime).toBe('14:30:00');
    expect(p.confidence).toBe(1.0);
  });
});
