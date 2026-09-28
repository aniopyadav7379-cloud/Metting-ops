import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { DragDropOverlay } from '../components/DragDropOverlay';
import { UploadTriageModal } from '../components/UploadTriageModal';
import { UploadTray } from '../components/UploadTray';
import { defaultActionForFile, detectFileKind, autoTitleFromFilename } from '../utils/uploads';

const startUploads = vi.fn();
const cancelUpload = vi.fn();
const openSession = vi.fn();
let mockedUploads: any[] = [];

vi.mock('../contexts/UploadsContext', () => ({
  useUploads: () => ({
    uploads: mockedUploads,
    startUploads,
    cancelUpload,
    openSession,
  }),
}));

function file(name: string, type: string, size = 1024) {
  return new File(['x'.repeat(size)], name, { type });
}

describe('uploads UI', () => {
  beforeEach(() => {
    startUploads.mockReset();
    cancelUpload.mockReset();
    openSession.mockReset();
    mockedUploads = [];
  });

  it('detects file types and default actions', () => {
    expect(detectFileKind(file('meeting.mp3', 'audio/mpeg'))).toBe('audio');
    expect(detectFileKind(file('recording.mov', 'video/quicktime'))).toBe('video');
    expect(detectFileKind(file('notes.pdf', 'application/pdf'))).toBe('image');
    expect(defaultActionForFile(file('meeting.wav', 'audio/wav'))).toBe('transcribe');
    expect(defaultActionForFile(file('notes.md', 'text/markdown'))).toBe('attach');
  });

  it('builds clean upload titles from filenames', () => {
    expect(autoTitleFromFilename('Zoom_2026-04-15_strategy-sync.mp3')).toBe('Strategy Sync');
  });

  it('shows the drag overlay and starts a single audio upload on drop', async () => {
    render(<DragDropOverlay />);
    const audio = file('call.wav', 'audio/wav');
    fireEvent.dragEnter(window, { dataTransfer: { types: ['Files'] } });
    expect(screen.getByText('Drop to upload')).toBeInTheDocument();
    fireEvent.drop(window, { dataTransfer: { types: ['Files'], files: [audio] } });
    await waitFor(() => expect(startUploads).toHaveBeenCalledWith([audio]));
    expect(screen.queryByText('Drop to upload')).not.toBeInTheDocument();
  });

  it('renders triage options for mixed drops', () => {
    render(
      <UploadTriageModal
        files={[file('meeting.mp4', 'video/mp4'), file('agenda.md', 'text/markdown')]}
        onClose={vi.fn()}
        onTranscribe={vi.fn()}
      />,
    );
    expect(screen.getByText('Extract audio + transcribe · Est. 1 min')).toBeInTheDocument();
    expect(screen.getByText('Attach to meeting... · Est. 1 min')).toBeInTheDocument();
  });

  it('renders tray progress, errors, and completed session navigation', () => {
    mockedUploads = [
      {
        localId: 'one',
        filename: 'team.wav',
        size: 2048,
        action: 'transcribe',
        stage: 'failed',
        progress: 45,
        error: 'Transcription failed',
        createdAt: Date.now(),
      },
      {
        localId: 'two',
        filename: 'done.wav',
        size: 1024,
        action: 'transcribe',
        stage: 'done',
        progress: 100,
        sessionId: 12,
        createdAt: Date.now(),
      },
    ];
    render(<UploadTray />);
    expect(screen.getByText('team.wav')).toBeInTheDocument();
    expect(screen.getByText('Transcription failed')).toBeInTheDocument();
    fireEvent.click(screen.getByText('done.wav'));
    expect(openSession).toHaveBeenCalled();
  });
});
