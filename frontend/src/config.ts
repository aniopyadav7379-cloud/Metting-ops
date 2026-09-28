// API Configuration

const isDevelopment = import.meta.env.DEV;

// Make API URL dynamic - evaluated each time it's accessed.
//
// Production cloud build (deploy/bigboy) serves the SPA and the FastAPI
// backend on the same host (meetingops.magicunicorn.dev) via oauth2-proxy +
// Traefik. So same-origin is the right default — no separate api.{domain}
// subdomain, no extra DNS, and the session cookie flows naturally.
//
// Build-time override remains available via VITE_API_URL for deployments
// that DO want a separate API host (legacy on-prem appliance setups).
const getApiUrl = () => {
  if (import.meta.env.VITE_API_URL) {
    return import.meta.env.VITE_API_URL;
  }

  // Local dev: backend lives on a different port than the Vite dev server.
  if (import.meta.env.DEV) {
    return `http://${window.location.hostname}:9050`;
  }

  // Production / preview: same-origin. window.location.origin returns
  // protocol + host + port, e.g. "https://meetingops.magicunicorn.dev".
  return window.location.origin;
};

// Helper to get WebSocket base URL - also dynamic
const getWsBaseUrl = () => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const apiUrl = getApiUrl();
  const host = apiUrl.replace('http://', '').replace('https://', '');
  return `${protocol}//${host}`;
};

export const config = {
  // Make apiBaseUrl a getter so it's evaluated dynamically
  get apiBaseUrl() {
    return getApiUrl();
  },
  
  // Add apiUrl alias for backward compatibility
  get apiUrl() {
    return getApiUrl();
  },
  
  apiEndpoints: {
    // Core endpoints
    status: '/api/status',
    sessions: '/api/recording-sessions',
    settings: '/api/settings',
    audioDevices: '/api/audio-devices',
    audioLevel: '/api/audio-level',
    systemInfo: '/api/system-info',
    
    // Simple recording endpoints
    simpleRecording: {
      sessions: '/api/simple/recording-sessions',
      start: (id: string) => `/api/simple/recording-sessions/${id}/start`,
      stop: (id: string) => `/api/simple/recording-sessions/${id}/stop`,
      session: (id: string) => `/api/simple/recording-sessions/${id}`,
      transcript: (id: string) => `/api/simple/recording-sessions/${id}/transcript`,
      export: (id: string) => `/api/simple/recording-sessions/${id}/export`,
      insights: (id: string) => `/api/simple/recording-sessions/${id}/insights`,
    },
    
    // Auth endpoints (updated for Authentik)
    auth: {
      me: '/api/auth/me', // Get current user from headers
      login: () => {
        const isSSO = window.location.protocol === 'https:' && window.location.hostname.includes('.');
        if (isSSO) {
          const domain = window.location.hostname.split('.').slice(1).join('.');
          return `https://auth.${domain}/if/flow/authentication/?next=${encodeURIComponent(window.location.href)}`;
        }
        return 'https://auth.meeting-ops.local/if/flow/authentication/';
      },
      logout: () => {
        const isSSO = window.location.protocol === 'https:' && window.location.hostname.includes('.');
        if (isSSO) {
          const domain = window.location.hostname.split('.').slice(1).join('.');
          return `https://auth.${domain}/if/flow/invalidation/`;
        }
        return 'https://auth.meeting-ops.local/if/flow/invalidation/';
      },
      profile: () => {
        const isSSO = window.location.protocol === 'https:' && window.location.hostname.includes('.');
        if (isSSO) {
          const domain = window.location.hostname.split('.').slice(1).join('.');
          return `https://auth.${domain}/api/v3/core/users/me/`;
        }
        return 'https://auth.meeting-ops.local/api/v3/core/users/me/';
      },
    },
    
    // Meeting management
    meetings: {
      analytics: '/api/meetings/analytics',
      insights: '/api/meetings/insights',
      speakers: '/api/meetings/speakers',
      keywords: '/api/meetings/keywords',
      summaries: '/api/meetings/summaries',
    },
    
    // System monitoring
    system: {
      npu: '/api/system/npu-status',
      services: '/api/system/services',
      metrics: '/api/system-info',
      logs: '/api/system/logs',
    },
    
    // AI/LLM endpoints
    ai: {
      models: '/api/ai/models',
      settings: '/api/ai/settings',
      transcriptionSettings: '/api/ai/transcription-settings',
      diarizationSettings: '/api/ai/diarization-settings',
    },
    
    // Agent System (new decoupled system)
    agents: {
      configurations: '/api/agents/configurations',
      create: '/api/agents/configurations',
      providers: '/api/agents/providers',
      execute: '/api/agents/execute/live',
      initialize: '/api/agents/initialize-defaults',
    },
    
    // Model Management (new system)
    models: {
      configurations: '/api/model-management/configurations',
      providers: '/api/model-management/providers',
      parameters: '/api/model-management/parameter-sets',
      status: '/api/model-management/status',
    },
    
    // File management
    files: {
      list: '/api/files',
      upload: '/api/files/upload',
      download: (id: string) => `/api/files/${id}/download`,
      delete: (id: string) => `/api/files/${id}`,
    },
    
    // Export endpoints
    export: {
      pdf: (id: string) => `/api/export/${id}/pdf`,
      docx: (id: string) => `/api/export/${id}/docx`,
      txt: (id: string) => `/api/export/${id}/txt`,
      batch: '/api/export/batch',
    },
    
    // User management
    users: {
      list: '/api/users',
      create: '/api/users',
      update: (id: string) => `/api/users/${id}`,
      delete: (id: string) => `/api/users/${id}`,
      roles: '/api/users/roles',
    },
  },
  
  wsEndpoints: {
    // Base URL for WebSocket connections
    get base() {
      return getWsBaseUrl();
    },
    
    // Core WebSocket endpoints
    stream: (sessionId: string) => `${getWsBaseUrl()}/ws/stream/${sessionId}`,
    transcription: (sessionId: string) => `${getWsBaseUrl()}/ws/transcription/${sessionId}`,
    audioLevels: () => `${getWsBaseUrl()}/ws/audio-levels`,
    systemMetrics: () => `${getWsBaseUrl()}/ws/system-metrics`,
    
    // Transcription loop (real-time transcription)
    transcriptionLoop: () => `${getWsBaseUrl()}/api/transcription-loop/ws`,
    
    // Time-shift recording
    timeshift: {
      start: () => `${getWsBaseUrl()}/ws/timeshift/start`,
      stop: () => `${getWsBaseUrl()}/ws/timeshift/stop`,
      stream: (sessionId: string) => `${getWsBaseUrl()}/ws/timeshift/${sessionId}`,
    }
  },
  
  // Polling intervals (ms)
  polling: {
    status: 5000,
    sessions: 10000,
    audioLevel: 100,
    systemMetrics: 2000,
    npuStatus: 3000,
  },
  
  // External service URLs (configurable)
  externalServices: {
    inferenceServer: import.meta.env.VITE_INFERENCE_URL || 'http://localhost:8080',
  }
};

export default config;

/**
 * v3.29.3: append the caller's JWT to a WebSocket URL as `?token=` (or `&token=`
 * if the URL already has a query string, e.g. the `?org=` from getOrgQueryUrl).
 * The browser WebSocket API can't set an Authorization header, so the meeting
 * sockets (transcription / audio-levels / transcription-auto) authenticate via
 * this query param — matching the backend `auth.ws_auth.enforce_ws_auth`
 * handshake check. No-op when no token is stored (the socket then relies on the
 * WS_REQUIRE_AUTH kill-switch / oauth2-proxy, i.e. the pre-v3.29.3 behaviour).
 */
export function appendWsToken(url: string): string {
  const token =
    (typeof localStorage !== 'undefined' && localStorage.getItem('access_token')) || '';
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}