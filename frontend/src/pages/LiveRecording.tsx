import React, { useState, useEffect, useRef } from 'react';
import { 
  Mic, 
  MicOff, 
  Play, 
  Pause, 
  FileText,
  ListChecks,
  Flag,
  Sparkles,
  Cpu,
  HardDrive,
  Check,
  CheckCircle,
  AlertCircle,
  Bot,
  Brain,
  Target,
  Zap,
  RotateCcw
} from 'lucide-react';
import { config, appendWsToken } from '../config';
import { showToast } from '../components/Toast';
import { useAuth } from '../contexts/AuthContext';
import { useOrg } from '../contexts/OrgContext';
import MobileLiveRecording from '../components/MobileLiveRecording';
import AlwaysOnControl from '../components/AlwaysOnControl';
import ConfirmModal from '../components/ConfirmModal';
import PipelineStatusPicker from '../components/PipelineStatusPicker';
import ServerLiveTranscript from '../components/ServerLiveTranscript';
import { useTierFeatures } from '../hooks/useTierFeatures';
import {
  isAlwaysOnActive,
  setRecordActive,
} from '../utils/recordingPresence';
import { useAlwaysOn } from '../contexts/AlwaysOnContext';

// Pull the action-items section out of a summary (slices join headers like
// "**Action Items**" / "## Next steps"); empty string when no such section.
function extractActionItems(text: string): string {
  const lines = text.split(/\r?\n/);
  const out: string[] = [];
  let inSection = false;
  for (const ln of lines) {
    const isActionHeader = /^\s*(\*\*|##+|#)?\s*(key\s+)?(action\s*items?|next\s*steps)\b/i.test(ln);
    const isOtherHeader = /^\s*(\*\*|##+)\s*\S/.test(ln) && !isActionHeader;
    if (isActionHeader) { inSection = true; continue; }
    if (inSection && isOtherHeader) break;
    if (inSection && ln.trim()) out.push(ln.trim());
  }
  return out.join('\n');
}

function downloadTextFile(filename: string, text: string) {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

interface SummaryFormats {
  executive: boolean;
  minutes: boolean;
  bullets: boolean;
  actions: boolean;
  decisions: boolean;
  tasks: boolean;
  transcript: boolean;
}

interface ActionItem {
  id: string;
  action: string;
  owner?: string;
  dueDate?: string;
  status: 'pending' | 'completed';
}

interface UnifiedAgent {
  id: string;
  name: string;
  description: string;
  provider_type: string;
  model_name: string;
  is_active: boolean;
}

interface ProgressiveSummaryMessage {
  type: 'progressive_summary';
  session_id: string;
  agent: {
    id: string;
    name: string;
    role: string;
  };
  summary_data: {
    word_count_at_summary: number;
    interval_used: number;
    next_interval: number;
    model_size: string;
    sections: Record<string, any>;
  };
  timestamp: string;
}

interface SessionData {
  id: string;
  title: string;
  startTime: string;
  duration: number;
  isRecording: boolean;
  transcript: string;
  isProcessingFinalSummary?: boolean;
  summary?: {
    executive?: string;
    bullets?: string[];
    actions?: ActionItem[];
    decisions?: string[];
    tasks?: Array<{task: string; assignee: string}>;
  };
}

export default function LiveRecording() {
  const { getOrgQueryUrl } = useOrg();
  // Check for existing recording session on mount
  const getStoredSession = () => {
    const stored = localStorage.getItem('activeRecordingSession');
    if (stored) {
      try {
        return JSON.parse(stored);
      } catch (e) {
        localStorage.removeItem('activeRecordingSession');
      }
    }
    return null;
  };

  // Check for last meeting summary (persists until cleared/logout)
  const getStoredSummary = () => {
    const stored = localStorage.getItem('lastMeetingSummary');
    if (stored) {
      try {
        const summary = JSON.parse(stored);
        // Only load if it's a valid summary structure
        if (summary && typeof summary === 'object' &&
            (summary.executive || summary.bullets || summary.actions || summary.decisions)) {
          return summary;
        } else {
          localStorage.removeItem('lastMeetingSummary');
        }
      } catch (e) {
        localStorage.removeItem('lastMeetingSummary');
      }
    }
    return null;
  };

  const storedSession = getStoredSession();
  
  const [isRecording, setIsRecording] = useState(storedSession?.isRecording || false);
  const [sessionData, setSessionData] = useState<SessionData | null>(storedSession);
  const [duration, setDuration] = useState(storedSession?.duration || 0);
  const [isProcessing, setIsProcessing] = useState(false);
  // Cross-surface collision modal — set when the user clicks Start
  // (legacy server recorder) while always-on is already capturing.
  const [collisionOpen, setCollisionOpen] = useState(false);
  const [summaryFormats, setSummaryFormats] = useState<SummaryFormats>({
    executive: true,
    minutes: false,
    bullets: true,
    actions: true,
    decisions: true,
    tasks: false,
    transcript: false
  });
  const [liveTranscript, setLiveTranscript] = useState<string[]>([]);
  const [autoSummary, setAutoSummary] = useState<any>(getStoredSummary());
  // Always-on state — Quick Actions must work for the always-on flow too,
  // where the legacy sessionData/autoSummary stay null (Ceejay 2026-07-16:
  // "Export Summary / Copy Action Items do not work").
  const alwaysOn = useAlwaysOn();
  const quickActionSummaryText: string = (() => {
    if (alwaysOn.summary.slices.length > 0) {
      return alwaysOn.summary.slices.map((s) => s.text).join('\n\n').trim();
    }
    if (autoSummary) {
      return (typeof autoSummary === 'string'
        ? autoSummary
        : autoSummary.executive || JSON.stringify(autoSummary, null, 2)
      ).trim();
    }
    if (sessionData?.summary) return String(sessionData.summary).trim();
    return '';
  })();
  const [wordCount, setWordCount] = useState(0);
  const [nextUpdateWords, setNextUpdateWords] = useState(500);
  const [activeAgent, setActiveAgent] = useState<UnifiedAgent | null>(null);
  const [progressiveData, setProgressiveData] = useState<{
    intervalUsed: number;
    nextInterval: number;
    modelSize: string;
  } | null>(null);
  const [summarySettings, setSummarySettings] = useState({
    enableLiveSummarization: localStorage.getItem('enableLiveSummarization') !== null 
      ? localStorage.getItem('enableLiveSummarization') === 'true'
      : true, // Default ON for new users
    enableFinalSummary: true,
    summaryTriggerMode: 'word_count', // 'word_count' or 'time'
    summaryWordCountInterval: 500, // Fixed 500 word intervals
    summaryUpdateInterval: 60 // seconds (for legacy time mode)
  });
  // Removed showLiveIntelligence - summaries now integrated into sections
  const [audioDevices, setAudioDevices] = useState<any[]>([]);
  const [selectedDevice, setSelectedDevice] = useState<string>('');
  // Initial labels shown until /api/system/pipeline resolves the live values.
  // Keep them aligned with what actually runs (Parakeet STT + Qwen 3.6 LLM) so
  // they don't flash stale model names.
  const [llmModelName, setLlmModelName] = useState<string>('Qwen 3.6 35B-A3B-Vision');
  const [sttModelName, setSttModelName] = useState<string>('Parakeet 1.1B');

  // Declutter: the summary-format toggles collapse behind a header
  // disclosure so the record control stays the focus. Defaults collapsed.
  const [showFormats, setShowFormats] = useState(false);

  // Always-on recording mode
  const { token } = useAuth();
  // Phase B.3 chunk D: gate the server-live transcript + speaker badges on
  // the tier_features.server_live flag. Free tier sees no change; enterprise
  // / pro / superuser get a second transcript pane fed by Phase B.2 + Sortformer.
  const { hasFeature } = useTierFeatures();
  const serverLiveEnabled = hasFeature('server_live');
  const [alwaysOnEnabled, setAlwaysOnEnabled] = useState(false);
  const [alwaysOnState, setAlwaysOnState] = useState<string>('IDLE');
  const [alwaysOnSessionId, setAlwaysOnSessionId] = useState<string | null>(null);
  const [alwaysOnMeetings, setAlwaysOnMeetings] = useState(0);
  const alwaysOnPollRef = useRef<NodeJS.Timeout | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const startTimeRef = useRef<Date | null>(storedSession && storedSession.startTime ? (() => {
    const date = new Date(storedSession.startTime);
    return isNaN(date.getTime()) ? null : date;
  })() : null);

  // Clear invalid summary data on mount
  useEffect(() => {
    const stored = localStorage.getItem('lastMeetingSummary');
    if (stored) {
      try {
        const summary = JSON.parse(stored);
        // Remove invalid summary structure
        if (summary && summary.type === 'progressive_trigger') {
          localStorage.removeItem('lastMeetingSummary');
          setAutoSummary(null);
        }
      } catch (e) {
        // Invalid JSON, clear it
        localStorage.removeItem('lastMeetingSummary');
      }
    }
  }, []); // Run once on mount

  // Save session state to localStorage whenever it changes
  useEffect(() => {
    if (sessionData) {
      const toStore = {
        ...sessionData,
        isRecording,
        duration,
        startTime: startTimeRef.current ? startTimeRef.current.toISOString() : sessionData.startTime
      };
      localStorage.setItem('activeRecordingSession', JSON.stringify(toStore));
    } else if (!isRecording) {
      localStorage.removeItem('activeRecordingSession');
    }
  }, [sessionData, isRecording, duration]);

  // Reconnect to existing session if stored
  useEffect(() => {
    if (storedSession && storedSession.isRecording && storedSession.id) {
      // Reconnect to enhanced WebSocket that provides both transcription and auto-summaries
      const wsUrl = appendWsToken(getOrgQueryUrl(`${config.wsEndpoints.base}/ws/transcription-auto/${storedSession.id}`));
      const ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          
          // Handle live transcription segments
          if (data.type === 'transcription') {
            const text = data.segment?.text || data.text || '';
            if (text.trim()) {
              setLiveTranscript(prev => [...prev, text]);
            }
          } 
          // Handle progressive summaries from backend agent system
          else if (data.type === 'progressive_summary') {
            // Handle both old and new formats
            let summaryContent, wordCount, nextInterval;
            
            if (data.summary_data) {
              // NEW unified agent format
              summaryContent = data.summary_data.sections;
              wordCount = data.summary_data.word_count_at_summary;
              nextInterval = data.summary_data.next_interval;
              
              // Update progressive interval data
              setProgressiveData({
                intervalUsed: data.summary_data.interval_used,
                nextInterval: data.summary_data.next_interval,
                modelSize: data.summary_data.model_size || 'Qwen 3.6 35B-A3B-Vision'
              });
            } else if (data.data) {
              // OLD format compatibility
              summaryContent = data.data.summary || data.data;
              wordCount = data.data.word_count || wordCount;
              nextInterval = data.data.next_interval || 500;
              
              // Set basic progressive data for old format
              setProgressiveData({
                intervalUsed: wordCount || 0,
                nextInterval: nextInterval,
                modelSize: 'Qwen 3.6 35B-A3B-Vision'
              });
            } else {
              return;
            }

            if (summaryContent) {
              setAutoSummary(summaryContent);
              
              // Persist summary to localStorage
              localStorage.setItem('lastMeetingSummary', JSON.stringify(summaryContent));
              localStorage.setItem('lastMeetingTimestamp', new Date().toISOString());
              
              // Update word count info
              if (wordCount) {
                setWordCount(wordCount);
              }
              if (nextInterval) {
                setNextUpdateWords(nextInterval);
              }
              
              setSessionData(prev => prev ? {
                ...prev,
                summary: summaryContent
              } : null);
            }
          }
          // Handle legacy auto_summary format for backwards compatibility
          else if (data.type === 'auto_summary') {
            setAutoSummary(data.summary);

            // Update word count info if provided
            if (data.word_count) {
              setWordCount(data.word_count);
            }
            if (data.next_update) {
              // Extract number from "in 500 words"
              const match = data.next_update.match(/(\d+)/);
              if (match) {
                setNextUpdateWords(parseInt(match[1]));
              }
            }
            
            setSessionData(prev => prev ? {
              ...prev,
              summary: data.summary
            } : null);
          } 
          // Handle progressive trigger notifications (just a notification, not a summary)
          else if (data.type === 'progressive_trigger') {
            // No action needed
          }
          // Status and other messages are silently ignored
        } catch (e) {
          // Silently ignore parse errors in reconnection handler
        }
      };
      ws.onclose = () => {
        console.warn('[Meeting-Ops] Transcription WebSocket closed during recording');
      };
      ws.onerror = () => {
        console.error('[Meeting-Ops] Transcription WebSocket error during recording');
      };
      wsRef.current = ws;

      // Audio levels are already connected via the always-on WebSocket
    }

    return () => {
      if (wsRef.current) wsRef.current.close();
      // Keep audio levels WebSocket running
    };
  }, []);

  // (Removed) the legacy `/ws/audio-levels` socket. It drove an on-page level
  // meter that was retired from the UI, yet it still opened on mount and
  // reconnect-stormed every 3s — which spammed the console on the native-OIDC
  // node, where the browser WebSocket carries the session cookie but not the
  // `?token=` JWT the WS auth expects. The live transcript is computed
  // in-browser and never needed this socket.

  // Check audio devices
  useEffect(() => {
    const checkDevices = async () => {
      try {
        const response = await fetch(`${config.apiUrl}/api/simple/audio-devices`);
        if (response.ok) {
          const data = await response.json();
          const devices = data.devices || data; // Handle both formats
          
          // Map devices to expected format
          const mappedDevices = devices.map((device: any) => ({
            id: device.deviceId || device.id || `hw:${device.index},0`,
            deviceId: device.deviceId || device.id || `hw:${device.index},0`,
            label: device.label || device.name || 'Unknown Device',
            name: device.name || device.label || 'Unknown Device',
            ...device
          }));
          
          setAudioDevices(mappedDevices);
          if (mappedDevices.length > 0 && !selectedDevice) {
            setSelectedDevice(mappedDevices[0].id || mappedDevices[0].deviceId);
          }
        }
      } catch (error) {
        // Audio devices not available
      }
    };
    checkDevices();
  }, []);

  // Duration timer
  useEffect(() => {
    if (isRecording && startTimeRef.current) {
      // Calculate initial duration if reconnecting
      const now = new Date();
      const elapsed = Math.floor((now.getTime() - startTimeRef.current.getTime()) / 1000);
      setDuration(elapsed);
      
      intervalRef.current = setInterval(() => {
        const currentTime = new Date();
        const elapsedSeconds = Math.floor((currentTime.getTime() - startTimeRef.current!.getTime()) / 1000);
        setDuration(elapsedSeconds);
      }, 1000);
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    }
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [isRecording]);

  // Fetch LLM model name from backend
  // Live pipeline status — STT engine, diarization backend, LLM model.
  // Replaces the legacy /api/settings/models read which only understood the
  // UC-1 NPU-flavored response shape; cloud build now exposes the truth at
  // /api/system/pipeline.
  const [pipelineStatus, setPipelineStatus] = useState<{
    stt: { engine: string; model: string; endpoint: string; ready: boolean; gpu?: string | null; label: string };
    diarization: { backend: string; endpoint: string; ready: boolean; gpu?: string | null; label: string; hf_token_required?: boolean };
    llm: { route: 'direct' | 'litellm'; model: string; endpoint: string; ready: boolean; label: string; thinking: boolean };
    build: 'cloud' | 'appliance' | string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;
    const fetchPipeline = async () => {
      try {
        const res = await fetch('/api/system/pipeline');
        if (cancelled) return;
        // Stop the refresh loop when auth has gone away — otherwise the
        // interval keeps spamming 401s every 30s while the user sees a
        // login prompt. The AuthContext stale-session handler picks up
        // the next failed call and prompts a re-auth.
        if (res.status === 401 || res.status === 403) {
          if (intervalId !== null) {
            clearInterval(intervalId);
            intervalId = null;
          }
          return;
        }
        if (res.ok) {
          const data = await res.json();
          setPipelineStatus(data);
          setLlmModelName(data.llm?.label || data.llm?.model || 'unknown');
          setSttModelName(data.stt?.label || data.stt?.model || 'unknown');
        }
      } catch {
        /* leave defaults */
      }
    };
    fetchPipeline();
    // Refresh every 30s so transient unreachability (e.g. midboy1 restart)
    // is reflected in the panel within a reasonable window. The handler
    // kills the interval if auth expires so we don't log-spam.
    intervalId = setInterval(fetchPipeline, 30_000);
    const id = intervalId;
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Fetch auto-summarization settings and active agent
  useEffect(() => {
    const fetchSettings = async () => {
      try {
        const response = await fetch(`${config.apiUrl}/api/auto-summary/settings`);
        if (response.ok) {
          const settings = await response.json();
          setSummarySettings(settings);
        }
      } catch (error) {
        // Use default summary settings
      }
    };

    const loadActiveAgent = async () => {
      try {
        const response = await fetch(`${config.apiUrl}/api/unified-agent/agent`);
        if (response.ok) {
          const agent = await response.json();
          setActiveAgent(agent);
        }
      } catch (error) {
        // Agent not available
      }
    };
    
    fetchSettings();
    loadActiveAgent();
  }, []);

  // Always-on mode: toggle and status polling
  const getAuthHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
  };

  const toggleAlwaysOn = async () => {
    try {
      if (alwaysOnEnabled) {
        // Stop always-on mode
        const res = await fetch(`${config.apiUrl}/api/simple/always-on/stop`, {
          method: 'POST',
          headers: getAuthHeaders(),
        });
        if (res.ok) {
          setAlwaysOnEnabled(false);
          setAlwaysOnState('IDLE');
          setAlwaysOnSessionId(null);
          // Stop polling
          if (alwaysOnPollRef.current) {
            clearInterval(alwaysOnPollRef.current);
            alwaysOnPollRef.current = null;
          }
        }
      } else {
        // Start always-on mode
        const res = await fetch(`${config.apiUrl}/api/simple/always-on/start`, {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({ device_id: selectedDevice || undefined }),
        });
        if (res.ok) {
          setAlwaysOnEnabled(true);
          setAlwaysOnState('IDLE');
        }
      }
    } catch (error) {
      // Always-on toggle failed
    }
  };

  // Poll always-on status every 5 seconds when enabled
  useEffect(() => {
    if (alwaysOnEnabled) {
      const pollStatus = async () => {
        try {
          const res = await fetch(`${config.apiUrl}/api/simple/always-on/status`, {
            headers: getAuthHeaders(),
          });
          if (res.ok) {
            const status = await res.json();
            setAlwaysOnState(status.state || 'IDLE');
            setAlwaysOnSessionId(status.current_session_id || null);
            setAlwaysOnMeetings(status.meetings_created || 0);

            // If always-on started recording, connect to its WebSocket for live transcription
            if (status.state === 'RECORDING' && status.current_session_id && !wsRef.current) {
              const wsUrl = appendWsToken(getOrgQueryUrl(`${config.wsEndpoints.base}/ws/transcription-auto/${status.current_session_id}`));
              const ws = new WebSocket(wsUrl);
              ws.onmessage = (event) => {
                try {
                  const data = JSON.parse(event.data);
                  if (data.type === 'transcription' || data.type === 'transcript_segment') {
                    const text = data.segment?.text || data.text || '';
                    if (text.trim()) {
                      setLiveTranscript(prev => [...prev, text]);
                    }
                  }
                } catch (e) {
                  // Ignore malformed messages
                }
              };
              wsRef.current = ws;
            }

            // If it stopped recording, clean up WebSocket
            if (status.state === 'IDLE' && wsRef.current) {
              wsRef.current.close();
              wsRef.current = null;
            }

            // If disabled externally
            if (!status.enabled) {
              setAlwaysOnEnabled(false);
              setAlwaysOnState('IDLE');
              setAlwaysOnSessionId(null);
              if (alwaysOnPollRef.current) {
                clearInterval(alwaysOnPollRef.current);
                alwaysOnPollRef.current = null;
              }
            }
          }
        } catch (error) {
          // Polling failed, will retry
        }
      };

      // Poll immediately, then every 5 seconds
      pollStatus();
      alwaysOnPollRef.current = setInterval(pollStatus, 5000);

      return () => {
        if (alwaysOnPollRef.current) {
          clearInterval(alwaysOnPollRef.current);
          alwaysOnPollRef.current = null;
        }
      };
    }
  }, [alwaysOnEnabled]);

  const formatDuration = (seconds: number) => {
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  // Format timestamp for transcript lines based on when they were spoken
  const formatTimestamp = (lineIndex: number) => {
    // Calculate approximate time based on line index and average speaking rate
    // Assuming average 150 words per minute, ~10 words per line
    const secondsPerLine = 4; // Approximate seconds per transcript line
    const totalSeconds = lineIndex * secondsPerLine;
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const formatTranscriptTimestamp = (lineIndex: number) => {
    // For live transcription, show elapsed time since recording started
    if (!startTimeRef.current) return '00:00';
    
    // Calculate seconds since start for this line
    // Assuming lines come in sequentially with ~5 second delays
    const secondsPerLine = 5;
    const elapsedSeconds = Math.min(lineIndex * secondsPerLine, duration);
    const mins = Math.floor(elapsedSeconds / 60);
    const secs = Math.floor(elapsedSeconds % 60);
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const startRecording = async () => {
    // Cross-surface guard. Always-on already captures the mic; running
    // the legacy server-side recorder at the same time would mean
    // double-prompting + two parallel streams. Bail out and surface a
    // modal instead.
    if (isAlwaysOnActive()) {
      setCollisionOpen(true);
      return;
    }
    setIsProcessing(true);
    try {
      // Create session
      const response = await fetch(`${config.apiUrl}/api/simple/recording-sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          name: `Meeting ${new Date().toLocaleString()}`,
          description: 'Recording from Meeting-Ops'
        })
      });
      
      if (!response.ok) {
        const error = await response.text();
        throw new Error(`Failed to create session: ${error}`);
      }
      const session = await response.json();

      // Connect to enhanced WebSocket BEFORE starting recording
      const wsUrl = appendWsToken(getOrgQueryUrl(`${config.wsEndpoints.base}/ws/transcription-auto/${session.id}`));
      const ws = new WebSocket(wsUrl);
      
      // Wait for WebSocket to connect before proceeding
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => {
          reject(new Error('WebSocket connection timeout'));
        }, 5000);
        
        ws.onopen = () => {
          clearTimeout(timeout);
          // Send initial message to register connection
          ws.send(JSON.stringify({
            type: 'register',
            session_id: session.id
          }));
          resolve();
        };

        ws.onerror = () => {
          clearTimeout(timeout);
          reject(new Error('WebSocket connection failed'));
        };
      });
      
      // NOW start the actual recording (after WebSocket is connected)
      const startResponse = await fetch(
        `${config.apiUrl}/api/simple/recording-sessions/${session.id}/start`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: selectedDevice || undefined })
        }
      );
      
      if (!startResponse.ok) {
        const error = await startResponse.text();
        ws.close(); // Close WebSocket if recording fails
        throw new Error(`Failed to start recording: ${error}`);
      }

      await startResponse.json();
      
      const newSession = {
        id: session.id,
        title: session.title || session.name,
        startTime: new Date().toISOString(),
        duration: 0,
        isRecording: true,
        transcript: ''
      };
      
      setSessionData(newSession);
      startTimeRef.current = new Date();
      // Don't reset autoSummary here - keep it if we have one
      // setAutoSummary(null); // Commented out to preserve summaries
      setWordCount(0); // Reset word count
      setNextUpdateWords(summarySettings.summaryWordCountInterval); // Reset next update
      
      // Set up message handler
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          // Handle live transcription segments - check multiple formats
          if (data.type === 'transcription' || data.type === 'transcript_segment') {
            const text = data.segment?.text || data.text || data.transcript || '';
            if (text.trim()) {
              setLiveTranscript(prev => [...prev, text]);

              // Update word count if provided
              if (data.word_count !== undefined) {
                setWordCount(data.word_count);
              } else if (data.segment_word_count !== undefined) {
                setWordCount(prev => prev + data.segment_word_count);
              } else {
                // Count words locally as fallback
                const words = text.split(/\s+/).filter((w: string) => w.length > 0).length;
                setWordCount(prev => prev + words);
              }
            }
          } 
          // Handle progressive summaries from backend agent system
          else if (data.type === 'progressive_summary') {
            // Handle both old and new formats
            let summaryContent, wordCount, nextInterval;

            if (data.summary_data) {
              // NEW unified agent format
              // Check if it's the new plain text format or structured format
              if (data.summary_data.text && typeof data.summary_data.text === 'string') {
                summaryContent = {
                  executive: data.summary_data.text,
                  bullets: [],
                  actions: [],
                  decisions: []
                };
                wordCount = data.summary_data.word_count || data.summary_data.word_count_at_summary;
              } else if (data.summary_data.sections) {
                summaryContent = data.summary_data.sections;
                wordCount = data.summary_data.word_count_at_summary;
              } else {
                // Fallback to direct summary_data if it has the fields
                summaryContent = data.summary_data;
                wordCount = data.summary_data.word_count_at_summary;
              }
              
              nextInterval = data.summary_data.next_interval;
              
              // Update progressive interval data
              setProgressiveData({
                intervalUsed: data.summary_data.interval_used,
                nextInterval: data.summary_data.next_interval,
                modelSize: data.summary_data.model_size
              });
            } else if (data.data) {
              // OLD format compatibility
              summaryContent = data.data.summary || data.data;
              wordCount = data.data.word_count || wordCount;
              nextInterval = data.data.next_interval || 500;
              
              // Set basic progressive data for old format
              setProgressiveData({
                intervalUsed: wordCount || 0,
                nextInterval: nextInterval,
                modelSize: 'Qwen 3.6 35B-A3B-Vision'
              });
            } else {
              return;
            }

            if (summaryContent) {
              setAutoSummary(summaryContent);
              
              // Persist summary to localStorage
              localStorage.setItem('lastMeetingSummary', JSON.stringify(summaryContent));
              localStorage.setItem('lastMeetingTimestamp', new Date().toISOString());
              
              // Update word count info
              if (wordCount) {
                setWordCount(wordCount);
              }
              if (nextInterval) {
                setNextUpdateWords(nextInterval);
              }
              
              setSessionData(prev => prev ? {
                ...prev,
                summary: summaryContent
              } : null);
            }
          }
          // Handle legacy auto_summary format for backwards compatibility
          else if (data.type === 'auto_summary') {
            setAutoSummary(data.summary);

            // Update word count info if provided
            if (data.word_count) {
              setWordCount(data.word_count);
            }
            if (data.next_update) {
              // Extract number from "in 500 words"
              const match = data.next_update.match(/(\d+)/);
              if (match) {
                setNextUpdateWords(parseInt(match[1]));
              }
            }
            
            setSessionData(prev => prev ? {
              ...prev,
              summary: data.summary
            } : null);
          }
          // Status and other messages are silently ignored
        } catch (e) {
          // Ignore malformed WebSocket messages
        }
      };
      ws.onclose = (event) => {
        if (isRecording) {
          console.warn('[Meeting-Ops] Transcription WebSocket lost during recording');
          showToast.warning('Connection to the server was lost. Your recording may still be running on the server — check the Sessions page.');
        }
      };
      ws.onerror = () => {
        console.error('[Meeting-Ops] Transcription WebSocket error');
      };
      wsRef.current = ws;

      // Audio levels are already connected via the always-on WebSocket

      setIsRecording(true);
      setDuration(0);
      // Cross-surface presence flag — always-on refuses to start while
      // this is set. Cleared in stopRecording / error / unmount.
      setRecordActive(true);
    } catch (error: any) {
      showToast.error(`Failed to start recording: ${error.message}`);
      setIsRecording(false);
      setRecordActive(false);
    } finally {
      setIsProcessing(false);
    }
  };

  const stopRecording = async () => {
    if (!sessionData) return;

    setIsProcessing(true);
    try {
      const stopHeaders: Record<string, string> = {};
      const stopToken = localStorage.getItem('access_token');
      if (stopToken) stopHeaders['Authorization'] = `Bearer ${stopToken}`;
      const stopResponse = await fetch(
        `${config.apiUrl}/api/simple/recording-sessions/${sessionData.id}/stop`,
        { method: 'POST', headers: stopHeaders }
      );

      // Durability fix: /stop no longer 500s when the server lost its
      // in-memory recorder state (backend restart / crash mid-recording).
      // It now returns 200 with one of three shapes — surface a CLEAR,
      // honest message for each instead of the old cryptic
      // "reset the connection" dead-end that told the user their
      // recording was simply gone.
      if (stopResponse.ok) {
        let stopBody: {
          status?: string;
          recovered?: boolean;
          recovered_from?: string;
          message?: string;
        } | null = null;
        try {
          stopBody = await stopResponse.clone().json();
        } catch {
          /* legacy/empty body — nothing to surface */
        }
        if (stopBody?.recovered) {
          // The recording survived an interruption and is now processing
          // from the audio that was persisted to disk as it recorded.
          showToast.success(
            stopBody.message
              || 'Recording recovered after an interruption and is now processing.',
          );
        } else if (stopBody?.status === 'no_audio') {
          // Honest: the server genuinely captured nothing for this session
          // (e.g. server-side audio disabled). Not an error, not a silent
          // loss — say so plainly.
          showToast.warning(
            stopBody.message
              || 'No audio was captured for this session on the server.',
          );
        }
      } else {
        // A genuine non-2xx (DB failure / unexpected exception after a
        // recovery attempt). Do NOT tell the user the recording is just
        // gone — partials are persisted server-side and the reprocess
        // recovery sweep can still pick them up. Point them at Sessions.
        showToast.warning(
          'There was a problem finishing this recording on the server. Any audio captured so far is saved — check the Sessions page; it may still finish processing.',
        );
      }

      // Continue cleanup regardless of the stop outcome.

      // Clean up transcription WebSocket only (keep audio levels running)
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      
      // If final summary is enabled, wait for it
      if (summarySettings.enableFinalSummary) {
        // Show loading state for final summary
        setSessionData(prev => prev ? {
          ...prev,
          isProcessingFinalSummary: true
        } : null);
        
        // Poll for final summary (backend generates it asynchronously)
        setTimeout(async () => {
          try {
            const pollToken = localStorage.getItem('access_token');
            const sessionResponse = await fetch(
              `${config.apiUrl}/api/simple/recording-sessions/${sessionData.id}`,
              { headers: pollToken ? { 'Authorization': `Bearer ${pollToken}` } : {} }
            );
            if (sessionResponse.ok) {
              const updatedSession = await sessionResponse.json();
              if (updatedSession.final_summary) {
                setAutoSummary(updatedSession.final_summary);
                setSessionData(prev => prev ? {
                  ...prev,
                  summary: updatedSession.final_summary,
                  isProcessingFinalSummary: false
                } : null);
              }
            }
          } catch (error) {
            // Final summary fetch failed, non-critical
          }
        }, 3000); // Give backend time to generate final summary
      }
      
      setIsRecording(false);
      // Cross-surface presence flag — server-side capture stopped, so
      // always-on is free to start again. setRecordActive is idempotent.
      setRecordActive(false);
      // Keep audio level monitoring active
      startTimeRef.current = null;
      localStorage.removeItem('activeRecordingSession');
      
      // Fetch final session data with AI summaries
      const finalToken = localStorage.getItem('access_token');
      const response = await fetch(`${config.apiUrl}/api/simple/recording-sessions/${sessionData.id}`, {
        headers: finalToken ? { 'Authorization': `Bearer ${finalToken}` } : {}
      });
      if (response.ok) {
        const finalData = await response.json();
        setSessionData(prev => ({
          ...prev!,
          isRecording: false,
          transcript: finalData.transcript_text || liveTranscript.join(' '),
          summary: finalData.ai_insights
        }));
      }
    } catch (error) {
      // The /stop request itself never completed (network drop). The
      // recording is NOT necessarily lost — chunks persisted server-side as
      // it recorded and the reprocess recovery sweep can still finish it.
      // Tell the user where to look instead of failing silently.
      showToast.warning(
        "Couldn't reach the server to finish this recording. Any audio captured so far is saved — check the Sessions page.",
      );
    } finally {
      setIsProcessing(false);
    }
  };

  const toggleFormat = (format: keyof SummaryFormats) => {
    setSummaryFormats(prev => ({ ...prev, [format]: !prev[format] }));
  };

  // Live transcript + live summary rendering (including the tail-follow
  // scroll behaviour and the summary-block flattening) now lives entirely
  // inside <AlwaysOnControl/>. The duplicate panels and their supporting
  // helpers that used to sit here were removed.

  return (
    <>
      <MobileLiveRecording />
      <div className="hidden md:block min-h-screen bg-gradient-to-b from-zinc-950 to-black text-zinc-100 p-4 pt-14 md:p-6 md:pt-6">
      {/* Header — compact so the record screen leads with recording, not chrome */}
      <div className="max-w-7xl mx-auto mb-3 md:mb-4">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
          <div>
            <h1 className="text-lg md:text-xl font-bold text-white flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-fuchsia-500 to-indigo-500 flex items-center justify-center shrink-0">
                <Sparkles className="w-4 h-4 text-white" />
              </div>
              Live Recording
            </h1>
          </div>

          {/* Format Toggles — collapsed behind a disclosure so the header
              stays clean and the record/transcript/summary panes are the
              focus. The toggles themselves (and all functionality) are
              unchanged, just tucked away until requested. */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setShowFormats((v) => !v)}
              aria-expanded={showFormats}
              className="inline-flex items-center gap-2 px-3 py-1.5 text-xs rounded-full border border-zinc-800 bg-zinc-900/50 text-zinc-300 hover:text-white hover:border-zinc-700 transition-all min-h-[36px]"
            >
              <FileText className="w-3.5 h-3.5" />
              Summary formats
              <span className="text-zinc-500">
                ({Object.values(summaryFormats).filter(Boolean).length})
              </span>
            </button>
            {showFormats && (
              <div className="mt-2 flex items-center gap-2 flex-wrap lg:absolute lg:right-0 lg:z-10 lg:mt-2 lg:max-w-md lg:justify-end lg:rounded-xl lg:border lg:border-zinc-800 lg:bg-zinc-900/95 lg:p-3 lg:shadow-xl lg:backdrop-blur">
                {Object.entries({
                  executive: 'Executive',
                  minutes: 'Minutes',
                  bullets: 'Bullets',
                  actions: 'Actions',
                  decisions: 'Decisions',
                  tasks: 'Tasks',
                  transcript: 'Transcript'
                }).map(([key, label]) => {
                  const selected = summaryFormats[key as keyof SummaryFormats];
                  return (
                    <button
                      key={key}
                      onClick={() => toggleFormat(key as keyof SummaryFormats)}
                      aria-pressed={selected}
                      className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-full border transition-all min-h-[36px] ${
                        selected
                          ? 'bg-gradient-to-r from-fuchsia-500 to-indigo-500 border-fuchsia-400/60 text-white shadow-md shadow-fuchsia-500/25 ring-1 ring-inset ring-white/15'
                          : 'bg-zinc-900/60 border-zinc-700 text-zinc-400 hover:text-zinc-100 hover:border-zinc-500 hover:bg-zinc-800/60'
                      }`}
                    >
                      {selected && <Check className="w-3.5 h-3.5 shrink-0" strokeWidth={3} />}
                      {label}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Main Content Grid */}
      <div className="max-w-7xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6">
        {/* Left: Recording Control & Summaries */}
        <div className="lg:col-span-2 space-y-4 md:space-y-6">
          <AlwaysOnControl />

          {/* The live metrics, listening pulse, live transcript and live
              summary now render entirely inside <AlwaysOnControl/> above.
              The duplicate focus-row + transcript + summary panels that
              used to sit here were removed so there is one live surface. */}

          {/* The old "Record from this browser" (DesktopBrowserRecorder) card
              was removed: it duplicated AlwaysOnControl but uploaded straight
              to the server to transcribe, bypassing the browser-first /
              privacy path and the free-tier gate. AlwaysOnControl is the one
              recording surface now. */}

          {/* Legacy server-side Recording Control. This card drives the
              backend ffmpeg/arecord pipeline which requires a microphone
              physically attached to the server. bigboy has no audio
              hardware, so it always errors. AlwaysOnControl replaces it for
              cloud deploys. Set SHOW_LEGACY_SERVER_RECORDING=true if we ever
              bring back an appliance/satellite build that has a local mic. */}
          {false && (
          <div className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-4 md:p-8">
            <div className="flex flex-col items-center">
              {/* Audio Device Selector */}
              {!isRecording && audioDevices.length > 1 && (
                <div className="w-full max-w-xs mb-6">
                  <label className="block text-xs text-zinc-400 mb-1.5 text-center">Audio Input</label>
                  <select
                    value={selectedDevice}
                    onChange={(e) => setSelectedDevice(e.target.value)}
                    className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 focus:border-purple-500 focus:outline-none"
                  >
                    {audioDevices.map((device) => (
                      <option key={device.id} value={device.id}>
                        {device.type === 'USB' ? '\uD83C\uDFA4 ' : '\uD83D\uDCBB '}{device.name || device.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {/* Big Record Button */}
              <button
                onClick={isRecording ? stopRecording : startRecording}
                disabled={audioDevices.length === 0 || isProcessing}
                className={`relative w-32 h-32 rounded-full transition-all transform hover:scale-105 ${
                  isRecording
                    ? 'bg-red-600 hover:bg-red-500 animate-pulse'
                    : 'bg-gradient-to-br from-fuchsia-600 to-indigo-600 hover:from-fuchsia-500 hover:to-indigo-500'
                } ${(audioDevices.length === 0 || isProcessing) ? 'opacity-50 cursor-not-allowed' : ''}`}
                title={isProcessing ? 'Processing...' : (isRecording ? 'Click to stop recording' : 'Click to start recording')}
              >
                <div className="absolute inset-0 flex items-center justify-center">
                  {isProcessing ? (
                    <div className="animate-spin rounded-full h-12 w-12 border-4 border-white border-t-transparent" />
                  ) : isRecording ? (
                    <Pause className="w-12 h-12 text-white" />
                  ) : (
                    <Mic className="w-12 h-12 text-white" />
                  )}
                </div>
                {isRecording && (
                  <div className="absolute inset-0 rounded-full border-4 border-red-400 animate-ping" />
                )}
              </button>
              
              {/* Status */}
              <div className="mt-6 text-center">
                <div className="text-2xl font-semibold text-white">
                  {isRecording ? 'Recording...' : (sessionData?.isRecording === false ? 'Recording Complete' : 'Ready to Record')}
                </div>
                {isRecording && (
                  <div className="text-4xl font-mono text-zinc-300 mt-2">
                    {formatDuration(duration)}
                  </div>
                )}
                {audioDevices.length === 0 && (
                  <div className="text-red-400 text-sm mt-2 flex items-center gap-2 justify-center">
                    <AlertCircle className="w-4 h-4" />
                    No audio device detected
                  </div>
                )}
                {/* New Recording button - shown after recording stops */}
                {sessionData && !isRecording && !isProcessing && (
                  <button
                    onClick={() => {
                      setSessionData(null);
                      setLiveTranscript([]);
                      setAutoSummary(null);
                      setDuration(0);
                      setProgressiveData(null);
                      startTimeRef.current = null;
                      localStorage.removeItem('activeRecordingSession');
                      localStorage.removeItem('lastMeetingSummary');
                      localStorage.removeItem('lastMeetingTimestamp');
                    }}
                    className="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 border border-zinc-600 text-zinc-300 hover:text-white transition-colors text-sm"
                  >
                    <RotateCcw className="w-4 h-4" />
                    New Recording
                  </button>
                )}
              </div>
              
              {/* Pipeline Status & Active Agent */}
              {(isRecording || sessionData) && (
                <div className="mt-6 space-y-4">
                  {/* Active Agent Display */}
                  {activeAgent && (
                    <div className="p-4 rounded-xl bg-zinc-900/50 border border-zinc-800 max-w-xs">
                      <div className="flex items-center gap-3 mb-2">
                        <Bot className="w-5 h-5 text-purple-400" />
                        <span className="text-sm font-medium text-zinc-200">Active Agent</span>
                      </div>
                      <div className="text-xs text-zinc-300">
                        <div className="font-medium">{activeAgent?.name}</div>
                        <div className="text-zinc-400 mt-1">{activeAgent?.description}</div>
                        <div className="flex items-center gap-1 mt-2">
                          <Target className="w-3 h-3 text-blue-400" />
                          <span className="capitalize">{activeAgent?.model_name}</span>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Progressive Interval Data */}
                  {progressiveData && (
                    <div className="p-3 rounded-xl bg-zinc-900/30 border border-zinc-700 max-w-xs">
                      <div className="flex items-center gap-2 mb-2">
                        <Brain className="w-4 h-4 text-green-400" />
                        <span className="text-xs font-medium text-zinc-300">Progressive Intervals</span>
                      </div>
                      <div className="text-xs text-zinc-400 space-y-1">
                        <div>Last: {progressiveData?.intervalUsed} words</div>
                        <div>Next: {progressiveData?.nextInterval} words</div>
                        <div>Model: {progressiveData?.modelSize}</div>
                      </div>
                    </div>
                  )}
                  
                  {/* Pipeline Status */}
                  {sessionData && (
                    <div className="p-4 rounded-xl bg-zinc-900/50 border border-zinc-800 max-w-xs">
                      <div className="flex items-center gap-3 mb-3">
                        <Cpu className="w-5 h-5 text-blue-400" />
                        <span className="text-sm font-medium text-zinc-200">Pipeline Status</span>
                      </div>
                      <div className="space-y-2 text-xs">
                        <div className="flex items-center justify-between">
                          <span className="text-zinc-400">Recording</span>
                          <span className={`flex items-center gap-1 ${
                            isRecording ? 'text-green-400' : 'text-green-600'
                          }`}>
                            <CheckCircle className="w-3 h-3" />
                            {isRecording ? 'Active' : 'Complete'}
                          </span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-zinc-400">Transcription</span>
                          <span className={`flex items-center gap-1 ${
                            liveTranscript.length > 0 ? 'text-green-400' : 
                            isRecording ? 'text-yellow-400' : 'text-zinc-500'
                          }`}>
                            {liveTranscript.length > 0 ? (
                              <><CheckCircle className="w-3 h-3" /> Active</>
                            ) : isRecording ? (
                              <><div className="w-3 h-3 border border-yellow-400 border-t-transparent rounded-full animate-spin"></div> Processing</>
                            ) : (
                              <><div className="w-3 h-3 border border-zinc-500 rounded-full"></div> Pending</>
                            )}
                          </span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-zinc-400">AI Summary</span>
                          <span className={`flex items-center gap-1 ${
                            autoSummary || sessionData?.summary ? 'text-green-400' : 
                            summarySettings.enableLiveSummarization && isRecording ? 'text-yellow-400' : 
                            'text-zinc-500'
                          }`}>
                            {autoSummary || sessionData?.summary ? (
                              <><CheckCircle className="w-3 h-3" /> Generated</>
                            ) : summarySettings.enableLiveSummarization && isRecording ? (
                              <><Brain className="w-3 h-3 animate-pulse" /> Analyzing</>
                            ) : (
                              <><div className="w-3 h-3 border border-zinc-500 rounded-full"></div> Pending</>
                            )}
                          </span>
                        </div>
                      </div>
                    </div>
                  )}
                  
                  {/* Live Summarization Toggle - Simple On/Off */}
                  <div className="p-4 rounded-xl bg-zinc-900/50 border border-zinc-800 max-w-xs">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <Sparkles className="w-5 h-5 text-yellow-400" />
                        <span className="text-sm font-medium text-zinc-200">Live Summarization</span>
                      </div>
                      <label className="relative inline-flex items-center cursor-pointer">
                        <input
                          type="checkbox"
                          checked={summarySettings.enableLiveSummarization}
                          onChange={async (e) => {
                            const newSettings = {
                              ...summarySettings,
                              enableLiveSummarization: e.target.checked
                            };
                            setSummarySettings(newSettings);
                            localStorage.setItem('enableLiveSummarization', e.target.checked.toString());
                            // Update backend settings
                            try {
                              await fetch(`${config.apiUrl}/api/auto-summary/settings`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(newSettings)
                              });
                            } catch (error) {
                              // Settings update failed
                            }
                          }}
                          className="sr-only peer"
                        />
                        <div className="w-11 h-6 bg-zinc-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-fuchsia-600"></div>
                      </label>
                    </div>
                    <div className="mt-3 text-xs text-zinc-400">
                      {summarySettings.enableLiveSummarization
                        ? `Every 500 words • ${llmModelName}`
                        : 'Disabled • Enable for live AI analysis'
                      }
                    </div>
                  </div>
                </div>
              )}
              
              {/* Audio Level Meter - Removed due to monitoring issues */}
            </div>
          </div>
          )}

          {/* Phase B.3 chunk D: server-live transcript with Sortformer
              speaker labels. Renders only for users whose tier exposes
              the `server_live` feature (enterprise / pro / superuser).
              Opens its own WS to /ws/sessions/{id}/live and runs in
              parallel with the existing /ws/transcription-auto pipeline
              — the user sees BOTH transcripts while recording. */}
          {serverLiveEnabled && sessionData?.id && (
            <div className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6">
              <ServerLiveTranscript
                sessionId={sessionData.id}
                enabled={isRecording}
              />
            </div>
          )}
          {/* v3.19 upgrade copy (audit §5). Pre-v3.19 we silently hid
              this slot when `server_live` was false, so free + standard
              Pro users never saw the affordance exists. Show an inline
              card explaining the upgrade — keeps the browser transcript
              framed as the private baseline. */}
          {!serverLiveEnabled && (
            <div className="rounded-2xl border border-purple-500/30 bg-purple-500/5 p-6">
              <div className="flex items-start gap-3">
                <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-purple-300" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-purple-100">
                    Pro live transcript
                  </div>
                  <p className="mt-1 text-xs leading-5 text-purple-200/80">
                    Pro live transcript adds lower-latency server
                    captions and speaker turns while your browser
                    transcript remains the private baseline.
                  </p>
                  <a
                    href="#/pricing"
                    className="mt-3 inline-flex items-center rounded-md bg-purple-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-purple-500"
                  >
                    View Pro pricing
                  </a>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Right: Status & Metrics */}
        <div className="space-y-4 md:space-y-6">
          {/* Agent & Pipeline Status — pipeline rows are now per-session
              dropdowns. See PipelineStatusPicker for the wiring; sources
              are the same as the AI Providers + In-Browser AI settings
              panels (no duplicate config). */}
          <PipelineStatusPicker
            isRecording={isRecording}
            recordingDurationLabel={formatDuration(duration)}
            agentName={activeAgent?.name}
            agentDescription={activeAgent?.description}
            agentModelLabel={activeAgent?.model_name}
          />

          {/* Live Summarization Progress — moved out of the pipeline card
              so the dropdown grid stays the dominant visual. Renders only
              while a session is active and live summarization is on. */}
          {isRecording && summarySettings.enableLiveSummarization && (
            <div className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-4">
              <div className="p-3 bg-gradient-to-r from-purple-900/30 to-blue-900/30 border border-purple-500/20 rounded-lg">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs text-purple-300">Live Summary Progress</span>
                  <span className="text-xs text-purple-400">{wordCount} / {Math.ceil(wordCount / 500) * 500}</span>
                </div>
                <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-purple-500 to-blue-500 transition-all duration-500"
                    style={{ width: `${Math.min((wordCount % 500) / 500 * 100, 100)}%` }}
                  />
                </div>
              </div>
            </div>
          )}

          {/* Session Info */}
          {sessionData && (
            <div className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6">
              <h3 className="text-lg font-semibold text-white mb-4">Session Info</h3>
              <div className="space-y-3">
                <div>
                  <div className="text-xs text-zinc-400">Session ID</div>
                  <div className="text-sm font-mono text-zinc-200 mt-1">{sessionData.id}</div>
                </div>
                <div>
                  <div className="text-xs text-zinc-400">Started</div>
                  <div className="text-sm text-zinc-200 mt-1">
                    {new Date(sessionData.startTime).toLocaleTimeString()}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-zinc-400">Duration</div>
                  <div className="text-sm text-zinc-200 mt-1">{formatDuration(duration)}</div>
                </div>
              </div>
            </div>
          )}

          {/* Quick Actions */}
          <div className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6">
            <h3 className="text-lg font-semibold text-white mb-4">Quick Actions</h3>
            <div className="space-y-2">
              <button
                className="w-full px-4 py-2 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-lg text-sm text-zinc-200 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={isRecording || (!sessionData?.id && !alwaysOnSessionId && !quickActionSummaryText)}
                title={
                  isRecording
                    ? 'Stop the recording first'
                    : (!sessionData?.id && !alwaysOnSessionId && !quickActionSummaryText)
                      ? 'Record a session first — the summary appears here when one exists'
                      : 'Download the summary (PDF when the server record exists, text otherwise)'
                }
                onClick={async () => {
                  const sid = sessionData?.id ?? alwaysOnSessionId;
                  if (sid) {
                    try {
                      const response = await fetch(
                        `${config.apiBaseUrl}/api/simple/recording-sessions/${sid}/download/summary/pdf`,
                        { headers: { 'Authorization': `Bearer ${token}` } }
                      );
                      if (!response.ok) throw new Error(`PDF export failed (${response.status})`);
                      const blob = await response.blob();
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = `meeting-summary.pdf`;
                      a.click();
                      URL.revokeObjectURL(url);
                      showToast.success('Summary PDF downloaded');
                      return;
                    } catch (err) {
                      console.error('PDF export error:', err);
                      // fall through to the local text export below
                    }
                  }
                  if (quickActionSummaryText) {
                    downloadTextFile('meeting-summary.md', quickActionSummaryText);
                    showToast.success(
                      sid
                        ? 'PDF unavailable — downloaded the live summary as text instead'
                        : 'Live summary downloaded as text'
                    );
                  } else {
                    showToast.error('No summary to export yet');
                  }
                }}
              >
                Export Summary
              </button>
              <button
                className="w-full px-4 py-2 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-lg text-sm text-zinc-200 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={!quickActionSummaryText}
                title={quickActionSummaryText
                  ? 'Copy the action items from the summary'
                  : 'Appears once a live summary exists'}
                onClick={() => {
                  if (!quickActionSummaryText) return;
                  const items = extractActionItems(quickActionSummaryText);
                  navigator.clipboard.writeText(items || quickActionSummaryText)
                    .then(() => showToast.success(items ? 'Action items copied' : 'No action-items section found — copied the whole summary'))
                    .catch(() => showToast.error('Could not copy — clipboard access was blocked'));
                }}
              >
                Copy Action Items
              </button>
              <button
                className="w-full px-4 py-2 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-lg text-sm text-zinc-200 transition-colors"
                onClick={() => { window.location.hash = '#/sessions'; }}
              >
                View All Sessions
              </button>
            </div>
          </div>
        </div>
      </div>
      </div>
      {/* Cross-surface collision guard for the legacy server-side
          recorder. Surfaces when the user clicks Start while always-on
          is already capturing. */}
      <ConfirmModal
        isOpen={collisionOpen}
        title="Always-on is already recording"
        description={(
          <>
            Always-on capture is currently running. Stop it from the
            Always-on panel, then click Start Recording again.
          </>
        )}
        confirmLabel="Got it"
        cancelLabel="Cancel"
        tone="danger"
        onConfirm={() => setCollisionOpen(false)}
        onCancel={() => setCollisionOpen(false)}
      />
    </>
  );
}
