import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Circle, Square, Clock } from 'lucide-react';
import { useRecording } from '../contexts/RecordingContext';
import { useAlwaysOn } from '../contexts/AlwaysOnContext';

// AlwaysOn states that mean "audio is being captured right now."
// Mirrors the gate used in PWAUpdate.tsx so we stay consistent.
const ACTIVE_ALWAYS_ON_STATES = ['starting', 'recording', 'paused', 'stopping'];

export const RecordingIndicator: React.FC = () => {
  const navigate = useNavigate();
  const { isRecording, recordingTime, activeSession } = useRecording();
  const { state: alwaysOnState } = useAlwaysOn();
  const [localTime, setLocalTime] = useState(recordingTime);

  // v3.22.4: warn the user before they refresh / close the tab while a
  // recording is live. MediaRecorder dies the instant the page unloads
  // (browsers free media streams when the document detaches), so a
  // reload would lose the live transcript + summary state AND end the
  // capture. Chunks uploaded so far are safe on the server, but the
  // current slice and anything since the last upload are gone.
  //
  // beforeunload's text isn't honored by modern browsers (they show a
  // generic "Leave site? Changes you made may not be saved" prompt),
  // but the prompt itself still fires when we set returnValue. Stays a
  // no-op when no recording is active so we don't nag.
  const recordingActive =
    isRecording || ACTIVE_ALWAYS_ON_STATES.includes(alwaysOnState);
  useEffect(() => {
    if (!recordingActive) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Returning a string is the older convention; modern browsers
      // require returnValue too. Setting both covers Firefox + Chromium.
      event.returnValue = '';
      return '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [recordingActive]);

  useEffect(() => {
    setLocalTime(recordingTime);
  }, [recordingTime]);

  useEffect(() => {
    if (isRecording) {
      const interval = setInterval(() => {
        setLocalTime(prev => prev + 1);
      }, 1000);
      return () => clearInterval(interval);
    }
  }, [isRecording]);

  const formatTime = (seconds: number) => {
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  if (!isRecording) return null;

  return (
    <button
      onClick={() => navigate('/record')}
      className="fixed top-4 right-4 z-50 bg-red-600 text-white px-4 py-2 rounded-lg shadow-lg cursor-pointer hover:bg-red-700 transition-colors animate-pulse"
      aria-label={`Recording in progress: ${activeSession?.name || 'session'} - ${formatTime(localTime)}`}
    >
      <div className="flex items-center gap-3">
        <Circle className="w-4 h-4 fill-white" />
        <span className="font-semibold">Recording</span>
        <div className="flex items-center gap-1">
          <Clock className="w-3 h-3" />
          <span className="font-mono text-sm">{formatTime(localTime)}</span>
        </div>
        {activeSession && (
          <span className="text-xs opacity-90 max-w-[200px] truncate">
            {activeSession.name}
          </span>
        )}
      </div>
    </button>
  );
};