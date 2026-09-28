import React, { createContext, useContext, useState, useEffect, type ReactNode } from 'react';
import { setRecordingActive } from '../utils/installFetchInterceptor';

interface TranscriptionSegment {
  id: string;
  text: string;
  timestamp: string;
  speaker?: string | null;
  confidence?: number;
  isFlagged?: boolean;
}

interface RecordingSession {
  id: string;
  name: string;
  status: 'idle' | 'recording' | 'paused' | 'stopped';
  startTime?: string;
  duration: number;
}

interface RecordingContextType {
  activeSession: RecordingSession | null;
  isRecording: boolean;
  recordingTime: number;
  transcriptions: TranscriptionSegment[];
  setActiveSession: (session: RecordingSession | null) => void;
  setIsRecording: (recording: boolean) => void;
  setRecordingTime: (time: number) => void;
  setTranscriptions: (transcriptions: TranscriptionSegment[]) => void;
  addTranscription: (segment: TranscriptionSegment) => void;
}

const RecordingContext = createContext<RecordingContextType | undefined>(undefined);

export const RecordingProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [activeSession, setActiveSession] = useState<RecordingSession | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [recordingTime, setRecordingTime] = useState(0);
  const [transcriptions, setTranscriptions] = useState<TranscriptionSegment[]>([]);

  // Load state from localStorage on mount
  useEffect(() => {
    const savedState = localStorage.getItem('recordingState');
    if (savedState) {
      try {
        const state = JSON.parse(savedState);
        if (state.activeSession) {
          setActiveSession(state.activeSession);
          setIsRecording(state.isRecording);
          setTranscriptions(state.transcriptions || []);
          
          // Calculate elapsed time if recording
          if (state.isRecording && state.activeSession?.startTime) {
            const startTime = new Date(state.activeSession.startTime).getTime();
            const elapsed = Math.floor((Date.now() - startTime) / 1000);
            setRecordingTime(elapsed);
          }
        }
      } catch (e) {
        console.error('Failed to load recording state:', e);
      }
    }
  }, []);

  // Save state to localStorage whenever it changes
  useEffect(() => {
    const state = {
      activeSession,
      isRecording,
      recordingTime,
      transcriptions
    };
    localStorage.setItem('recordingState', JSON.stringify(state));
  }, [activeSession, isRecording, recordingTime, transcriptions]);

  // Tell the fetch interceptor whether we're live, so a stale-session 302
  // during the recording defers its redirect instead of killing the capture.
  useEffect(() => {
    setRecordingActive('recordingContext', isRecording);
    return () => setRecordingActive('recordingContext', false);
  }, [isRecording]);

  const addTranscription = (segment: TranscriptionSegment) => {
    setTranscriptions(prev => {
      // Check for duplicates by ID or by text and timestamp proximity
      const isDuplicate = prev.some(existing => 
        existing.id === segment.id ||
        (existing.text === segment.text && 
         Math.abs(new Date(existing.timestamp).getTime() - new Date(segment.timestamp).getTime()) < 1000)
      );
      
      if (isDuplicate) {
        return prev;
      }
      
      return [...prev, segment];
    });
  };

  return (
    <RecordingContext.Provider
      value={{
        activeSession,
        isRecording,
        recordingTime,
        transcriptions,
        setActiveSession,
        setIsRecording,
        setRecordingTime,
        setTranscriptions,
        addTranscription
      }}
    >
      {children}
    </RecordingContext.Provider>
  );
};

export const useRecording = () => {
  const context = useContext(RecordingContext);
  if (context === undefined) {
    throw new Error('useRecording must be used within a RecordingProvider');
  }
  return context;
};
