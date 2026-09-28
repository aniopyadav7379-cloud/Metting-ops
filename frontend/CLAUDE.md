# Meeting-Ops Frontend Development Guide

## Quick Start
```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0   # Dev server on port 7777
npm run build                    # Production build (0 TS errors)
npx vitest run                   # Run tests (23 pass)
```

**Login**: admin / admin123
**Backend**: http://localhost:9050

## Architecture

### Router (AppRouterSimplified.tsx)
Mobile-responsive sidebar nav with hamburger menu on small screens.

| Route | Component | Purpose |
|-------|-----------|---------|
| /login | Login.tsx (component) | Authentication |
| /record | LiveRecording.tsx | Recording + live transcription |
| /sessions | Sessions.tsx | Session list + search + pagination |
| /sessions/:id | SessionDetails.tsx | Playback + transcript + export + AI chat |
| /settings | SettingsEnhanced.tsx | System settings + vocabulary |
| /admin/agents | AgentDashboard.tsx | AI agent management |
| /admin/agents-old | AgentConfiguration.tsx | Legacy agent config |

### Shared Components (src/components/)
- `Login.tsx` - Login form
- `ErrorMessage.tsx` - Error display
- `LoadingSpinner.tsx` - Loading indicator
- `RecordingIndicator.tsx` - Global recording status (navigates to /record)
- `SessionCreator.tsx` - Session creation dialog
- `Toast.tsx` - Toast notification utility (wraps react-toastify)

### Contexts
- `AuthContext.tsx` - JWT auth state + login/logout
- `ThemeContext.tsx` - Dark/light theme
- `RecordingContext.tsx` - Recording state

### Hooks
- `useKeyboardShortcuts.ts` - Keyboard shortcut handling

### Config
- `config.ts` - API URL construction (adapts to hostname)

## Features

### Sessions Page
- Server-side full-text search (debounced, calls `/api/simple/recording-sessions/search`)
- Client-side filtering by status and date
- Pagination (12 per page) with "Load More" alternative
- Bulk select + bulk delete
- Grid and list views
- Toast notifications for actions

### Session Details Page
- Audio player with transcript
- PDF/DOCX/TXT/SRT export (real backend generation)
- AI Chat panel (collapsible sidebar, real LLM responses)
- Real AI Insights sidebar (topics from summary, real sentiment, actual speaking time from segments)
- Transcript search and speaker filter

### Settings Page
- Mobile-responsive with horizontal scrollable tabs
- Custom vocabulary management (add/delete terms)
- Audio, AI, export, network settings

### Mobile Responsiveness
- All pages responsive with breakpoints at `sm`, `md`, `lg`
- Sidebar collapses to hamburger menu on mobile
- Touch-friendly targets (min-h-[44px])
- Content padding adjusts for hamburger button clearance

## API Integration
```typescript
// Recording sessions
POST /api/simple/recording-sessions           // Create
POST /api/simple/recording-sessions/{id}/start // Start recording
POST /api/simple/recording-sessions/{id}/stop  // Stop recording
GET  /api/simple/recording-sessions           // List
GET  /api/simple/recording-sessions/{id}      // Get with transcript
GET  /api/simple/recording-sessions/search?q= // Full-text search
GET  /api/simple/recording-sessions/{id}/download/audio          // WAV
GET  /api/simple/recording-sessions/{id}/download/summary/pdf    // PDF
GET  /api/simple/recording-sessions/{id}/download/summary/docx   // DOCX

// AI Chat
POST /api/ai-chat/sessions/{id}/messages      // Send message, get LLM response

// WebSockets
ws://host:9050/ws/transcription/{session_id}  // Live transcription
ws://host:9050/ws/audio-levels                // Audio levels
ws://host:9050/ws/auto-summary/{session_id}   // AI summaries
```

## Testing
```bash
npx vitest run  # 23 tests across 5 files

# Test files:
# Login.test.tsx (4 tests) - form render, submit
# Sessions.test.tsx (3 tests) - list render, fetch
# config.test.ts (4 tests) - URL construction
# AuthContext.test.tsx (2 tests) - context values
# errorHandling.test.ts (10 tests) - error utilities
```

## Styling
- TailwindCSS with dark theme, purple/fuchsia accents
- Cards: `bg-gray-800 rounded-lg p-4 sm:p-6`
- Buttons: `bg-purple-600 hover:bg-purple-700`
- Responsive spacing: `gap-4 md:gap-6`

## File Structure
```
frontend/src/
  App.tsx                    # Root (ThemeContext + RecordingContext + ToastContainer)
  AppRouterSimplified.tsx    # Router (7 routes, mobile sidebar)
  config.ts                  # API URLs
  pages/
    LiveRecording.tsx        # Recording page (responsive)
    Sessions.tsx             # Sessions (search + pagination + bulk)
    SessionDetails.tsx       # Details (export + AI chat + real insights)
    SettingsEnhanced.tsx     # Settings (vocabulary + responsive tabs)
    admin/
      AgentDashboard.tsx
      AgentConfiguration.tsx
      AgentEditor.tsx
  components/                # 6 shared components
  contexts/                  # 3 context providers
  hooks/                     # 1 hook
  __tests__/                 # 4 test files
```
