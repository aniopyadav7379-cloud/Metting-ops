import React, { Suspense, lazy, useState, useEffect, useCallback } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './contexts/AuthContext';
import { useOrg } from './contexts/OrgContext';
import { useRecording } from './contexts/RecordingContext';
import { redirectToLogin } from './utils/loginRedirect';
import { Login } from './components/Login';
import { SharedSessionRedirect } from './components/SharedSessionRedirect';
import LegalFooter from './components/LegalFooter';
import { useAlwaysOn } from './contexts/AlwaysOnContext';
import { Menu, X, Circle, Clock, DoorOpen, ShieldCheck, HelpCircle, Lock, FolderUp, ChevronLeft, ChevronRight, LogOut, Rocket, GraduationCap, Scale, Milestone } from 'lucide-react';
import { isAdminRole } from './utils/roles';
import { ErrorBoundary } from './components/ErrorBoundary';

// Keep the authenticated shell cheap: recording, report, graph and model UI
// are loaded only when their owning route opens. This matters even though the
// model runtimes themselves are dynamic imports, because a static route import
// makes every page component (and its feature dependencies) part of the boot
// graph.
const LiveRecording = lazy(() => import('./pages/LiveRecording'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const ActionItems = lazy(() => import('./pages/ActionItems'));
const DecisionsRisks = lazy(() => import('./pages/DecisionsRisks'));
const Lectures = lazy(() => import('./pages/Lectures'));
const ImportantMoments = lazy(() => import('./pages/ImportantMoments'));
const RAGChat = lazy(() => import('./pages/RAGChat'));
const DigestsPage = lazy(() => import('./pages/Digests'));
const KnowledgeGraph = lazy(() => import('./pages/KnowledgeGraph'));
const Sessions = lazy(() => import('./pages/Sessions').then(({ Sessions }) => ({ default: Sessions })));
const SessionDetails = lazy(() => import('./pages/SessionDetails').then(({ SessionDetails }) => ({ default: SessionDetails })));
const SpeakerLibrary = lazy(() => import('./pages/SpeakerLibrary').then(({ SpeakerLibrary }) => ({ default: SpeakerLibrary })));
const SettingsEnhanced = lazy(() => import('./pages/SettingsEnhanced'));
const AgentDashboard = lazy(() => import('./pages/admin/AgentDashboard').then(({ AgentDashboard }) => ({ default: AgentDashboard })));
const CompsAdmin = lazy(() => import('./pages/admin/CompsAdmin'));
const Rooms = lazy(() => import('./pages/Rooms'));
const RoomSetupWizard = lazy(() => import('./pages/RoomSetupWizard'));
const RoomDetail = lazy(() => import('./pages/RoomDetail'));
const Help = lazy(() => import('./pages/Help'));
const LocalSessions = lazy(() => import('./pages/LocalSessions'));
const Import = lazy(() => import('./pages/Import'));
const StreamingTest = lazy(() => import('./pages/StreamingTest'));
const AdminBulkImport = lazy(() => import('./pages/AdminBulkImport'));
const Signup = lazy(() => import('./pages/Signup'));
const Pricing = lazy(() => import('./pages/Pricing'));
const Landing = lazy(() => import('./pages/Landing'));
const ForgotPassword = lazy(() => import('./pages/ForgotPassword'));
const ResetPassword = lazy(() => import('./pages/ResetPassword'));
const Founding100 = lazy(() => import('./pages/Founding100'));
const Contact = lazy(() => import('./pages/Contact'));
const Terms = lazy(() => import('./pages/Terms'));
const Privacy = lazy(() => import('./pages/Privacy'));
const AUP = lazy(() => import('./pages/AUP'));

function RouteLoadingFallback() {
  return <div className="min-h-[12rem] animate-pulse p-6 text-sm text-zinc-500">Loading…</div>;
}

// Desktop sidebar collapsed-state persistence. Mobile open/close is
// ephemeral (sidebarOpen), but the desktop collapse preference must
// survive reloads, so it's mirrored to localStorage under this key.
const SIDEBAR_COLLAPSED_KEY = 'mo-sidebar-collapsed';

function readSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true';
  } catch {
    // Private-mode / disabled storage — default to expanded.
    return false;
  }
}

// Layout wrapper with simplified navigation
function AppLayout({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const { organizations, activeOrganization, switchOrganization } = useOrg();
  // Lock the workspace switcher while a recording is in flight. The recording's
  // org is pinned at start, so switching mid-recording only causes confusion
  // (and was how a stray session landed under the wrong workspace).
  const { state: alwaysOnState } = useAlwaysOn();
  const isRecording =
    alwaysOnState === 'recording'
    || alwaysOnState === 'paused'
    || alwaysOnState === 'starting'
    || alwaysOnState === 'stopping';
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Desktop-only collapse. Lazy-init from localStorage so the first
  // paint already reflects the persisted preference (no expand→collapse
  // flash on reload).
  const [collapsed, setCollapsed] = useState<boolean>(readSidebarCollapsed);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? 'true' : 'false');
    } catch {
      // ignore storage failures — collapse still works for the session.
    }
  }, [collapsed]);

  const toggleCollapsed = useCallback(() => setCollapsed((value) => !value), []);

  return (
    <div className="flex h-screen bg-gradient-to-b from-zinc-950 to-black">
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/60 z-40 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Mobile hamburger button */}
      <button
        onClick={() => setSidebarOpen(true)}
        className="fixed top-4 left-4 z-50 p-2 rounded-lg bg-zinc-900 border border-zinc-700 text-zinc-300 hover:text-white hover:bg-zinc-800 transition-colors md:hidden"
        aria-label="Open menu"
      >
        <Menu className="w-6 h-6" />
      </button>

      <SimplifiedNavigation
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        collapsed={collapsed}
        onToggleCollapse={toggleCollapsed}
      />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-30 flex items-center justify-end gap-3 border-b border-zinc-800 bg-black/80 px-4 py-3 backdrop-blur">
          <div className="mr-auto pl-12 text-sm text-zinc-500 md:pl-0">
            {activeOrganization ? `Workspace: ${activeOrganization.name}` : 'Workspace'}
          </div>
          <div className="min-w-0">
            <label className="sr-only" htmlFor="org-switcher">Organization</label>
            <select
              id="org-switcher"
              value={activeOrganization?.slug ?? ''}
              onChange={(event) => switchOrganization(event.target.value)}
              disabled={isRecording}
              title={isRecording ? 'Stop the current recording to switch workspace' : undefined}
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none transition focus:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {organizations.map((organization) => (
                <option key={organization.slug} value={organization.slug}>
                  {organization.name} ({organization.role})
                </option>
              ))}
            </select>
          </div>
          <div className="hidden text-right md:block">
            <div className="text-sm text-zinc-100">{user?.full_name || user?.email || user?.username}</div>
            <div className="text-xs text-zinc-500">{activeOrganization?.role || user?.role || 'user'}</div>
          </div>
        </header>
        {children}
        {/* v3.22.5: ecosystem-wide legal + support footer. Renders
            inside the authed shell so every page exposes Terms,
            Privacy, Acceptable Use, and Contact Support without
            coupling to any specific page layout. Anonymous marketing
            surfaces (Landing, Signup, Login, Pricing) embed the same
            <LegalFooter /> directly. */}
        <LegalFooter />
      </main>
    </div>
  );
}

// Simplified Navigation Component
function SimplifiedNavigation({
  isOpen,
  onClose,
  collapsed,
  onToggleCollapse,
}: {
  isOpen: boolean;
  onClose: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
}) {
  const { logout, user } = useAuth();
  const { activeOrganization } = useOrg();
  const { isRecording, recordingTime } = useRecording();
  const location = window.location.hash.replace('#', '');

  // Admin visibility is derived from the ACTIVE org's role + the
  // global superuser flag. Switching org rebinds this immediately —
  // a user who's admin in Magic Unicorn but member in Aaron-personal
  // sees the Admin section only when Magic Unicorn is active.
  const isAdmin = isAdminRole(activeOrganization?.role, user?.is_superuser);

  // navItems supports either an emoji string or a lucide-react icon
  // component. The Rooms entry uses the lucide DoorOpen glyph so it
  // stays visually anchored to the actions sidebar style; everything
  // else keeps its existing emoji to avoid a global cosmetic change.
  type NavItem = {
    path: string;
    label: string;
    icon: string | React.ComponentType<{ className?: string }>;
    primary?: boolean;
  };

  // Workspace-level nav — visible to everyone. Aaron's directive is
  // that admin items move OUT of the main list and into their own
  // section, so Speakers / Rooms (the multi-tenant configuration
  // surfaces) get pushed to the admin section below.
  // Knowledge Graph page is now GA. VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED=true is set
  // in our deploy envs so the nav item ships on by default; the flag is retained
  // only as a kill-switch (unset / non-"true" hides the nav item). The backend
  // gates the endpoint by tier, so typing the URL manually still resolves safely.
  const knowledgeGraphEnabled =
    String(
      (import.meta as unknown as { env?: Record<string, string | undefined> }).env
        ?.VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED || '',
    ).toLowerCase() === 'true';

  const navItems: NavItem[] = [
    { path: '/record', label: 'Record', icon: '🎙️', primary: true },
    { path: '/dashboard', label: 'Dashboard', icon: '🏠' },
    { path: '/sessions', label: 'Sessions', icon: '📁' },
    { path: '/lectures', label: 'Lectures', icon: GraduationCap },
    { path: '/action-items', label: 'Action items', icon: '✅' },
    { path: '/decisions-risks', label: 'Decisions & Risks', icon: Scale },
    { path: '/moments', label: 'Important Moments', icon: Milestone },
    { path: '/import', label: 'Import / Export', icon: FolderUp },
    { path: '/local-sessions', label: 'Local Sessions', icon: Lock },
    { path: '/rag', label: 'AI Chat', icon: '💬' },
    ...(knowledgeGraphEnabled
      ? [{ path: '/knowledge-graph', label: 'Knowledge Graph', icon: '🕸️' } as NavItem]
      : []),
    { path: '/digests', label: 'Digests', icon: '📊' },
    { path: '/settings', label: 'Settings', icon: '⚙️' },
    { path: '/help', label: 'Help', icon: HelpCircle },
  ];
  // v3.18.3: Room mode kill-switch. Cloud / VPS deploys have no physical
  // mic, so the rooms surface is hidden when VITE_ROOM_MODE_ENABLED=false.
  // Default ON (preserves bigboy appliance behavior). Backend also gates
  // the endpoints — typing /rooms manually will land a 503 page.
  const roomModeEnabled =
    (import.meta as unknown as { env?: Record<string, string | undefined> }).env
      ?.VITE_ROOM_MODE_ENABLED?.toLowerCase() !== 'false';

  // Admin-only nav — entirely hidden for non-admin users. Even the
  // section heading goes away when isAdmin is false; an empty
  // labeled bucket is noisy and Aaron wants the cleaner non-admin
  // view to land for regular users.
  //
  // Self-serve consumers are org-admins BY DESIGN (everyone owns their
  // private personal workspace), so `isAdmin` alone would show every
  // fresh signup an "Admin" section with appliance/fleet surfaces
  // (Rooms, Agents, Bulk Import) that only make sense on enterprise
  // deployments. Gate those on the active org's plan; consumer
  // workspaces keep just Speakers under a "Workspace" heading so a new
  // free-tier user never reads as a system admin.
  const isEnterpriseWorkspace =
    activeOrganization?.plan === 'enterprise' || Boolean(user?.is_superuser);
  const adminNavItems: NavItem[] = [
    ...(roomModeEnabled && isEnterpriseWorkspace
      ? [{ path: '/rooms', label: 'Rooms', icon: DoorOpen } as NavItem]
      : []),
    { path: '/speakers', label: 'Speakers', icon: '🗣️' },
    ...(isEnterpriseWorkspace
      ? [
          { path: '/admin/agents', label: 'Agents', icon: '🤖' } as NavItem,
          { path: '/admin/bulk-import', label: 'Bulk Import', icon: FolderUp } as NavItem,
        ]
      : []),
    // Launch console — comps + invite-code oversight. Platform-superuser only
    // (the backend /api/admin/comps* + /api/admin/invite-codes are superuser
    // gated), so it never shows for a self-serve org-admin.
    ...(user?.is_superuser
      ? [{ path: '/admin/comps', label: 'Launch', icon: Rocket } as NavItem]
      : []),
  ];

  const formatTime = (seconds: number) => {
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  // When collapsed (desktop rail), each item centers its icon and hides
  // the text label. The accessible name is preserved via title +
  // aria-label so the icon-only rail stays usable/screenreadable. The
  // mobile drawer always renders the full-width labelled item, so the
  // collapsed styling is scoped to the md+ breakpoint only.
  const renderNavItem = (item: NavItem) => {
    const Icon = typeof item.icon === 'string' ? null : item.icon;
    const glyph = Icon ? (
      <Icon className="w-5 h-5 shrink-0" />
    ) : (
      <span className="shrink-0">{item.icon as string}</span>
    );
    if (item.primary) {
      const active = location.startsWith(item.path);
      return (
        <a
          key={item.path}
          href={`#${item.path}`}
          onClick={onClose}
          title={collapsed ? item.label : undefined}
          aria-label={item.label}
          className={`flex items-center gap-3 px-3 py-3 mb-1 rounded-lg min-h-[48px] font-semibold text-white shadow-lg shadow-fuchsia-900/30 transition-colors ${
            collapsed ? 'md:justify-center md:px-0' : ''
          } ${
            active
              ? 'bg-gradient-to-r from-fuchsia-500 to-indigo-500'
              : 'bg-gradient-to-r from-fuchsia-600 to-indigo-600 hover:from-fuchsia-500 hover:to-indigo-500'
          }`}
        >
          {glyph}
          <span className={collapsed ? 'md:hidden' : ''}>{item.label}</span>
        </a>
      );
    }
    return (
      <a
        key={item.path}
        href={`#${item.path}`}
        onClick={onClose}
        title={collapsed ? item.label : undefined}
        aria-label={item.label}
        className={`flex items-center gap-3 px-3 py-2 rounded-lg transition-colors min-h-[44px] ${
          collapsed ? 'md:justify-center md:px-0' : ''
        } ${
          (item.path === '/' ? location === item.path : location.startsWith(item.path))
            ? 'bg-zinc-800 text-white'
            : 'text-zinc-400 hover:text-white hover:bg-zinc-900'
        }`}
      >
        {glyph}
        <span className={collapsed ? 'md:hidden' : ''}>{item.label}</span>
      </a>
    );
  };

  return (
    <nav
      className={`fixed top-0 left-0 h-full w-64 bg-zinc-950 border-r border-zinc-800 p-4 z-50 transform transition-all duration-200 ease-in-out
        ${isOpen ? 'translate-x-0' : '-translate-x-full'}
        md:relative md:translate-x-0 md:flex md:flex-col
        ${collapsed ? 'md:w-16 md:px-2' : 'md:w-64'}`}
    >
      {/* Close button for mobile */}
      <button
        onClick={onClose}
        className="absolute top-4 right-4 p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors md:hidden"
        aria-label="Close menu"
      >
        <X className="w-5 h-5" />
      </button>

      {/* Header: logo + brand. The brand text hides on the collapsed
          desktop rail; the logo tile stays as the persistent anchor.
          The desktop collapse/expand chevron lives top-right (md+ only)
          and is the primary control for narrowing/restoring the rail. */}
      <div className="mb-8">
        <div className={`flex items-center gap-3 ${collapsed ? 'md:justify-center md:gap-0' : ''}`}>
          <div className="w-10 h-10 rounded-xl overflow-hidden shrink-0 ring-1 ring-[#d4af37]/40 bg-[#0a1228] flex items-center justify-center">
            <img src="/brand/meeting-ops-mark.png?v=3" alt="Meeting-Ops" className="w-full h-full object-cover" />
          </div>
          <div className={collapsed ? 'md:hidden' : ''}>
            <div className="text-white font-semibold tracking-tight">Meeting-Ops</div>
            <div className="text-xs text-zinc-400 leading-snug">Intelligence for your conversations</div>
          </div>
          {/* Collapse chevron — desktop only, shown when expanded. */}
          {!collapsed && (
            <button
              onClick={onToggleCollapse}
              className="ml-auto hidden md:flex p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors"
              aria-label="Collapse sidebar"
              title="Collapse sidebar"
            >
              <ChevronLeft className="w-5 h-5" />
            </button>
          )}
        </div>
        {/* Expand control on the collapsed rail — centered under the
            logo so the rail always offers a one-click way back. */}
        {collapsed && (
          <button
            onClick={onToggleCollapse}
            className="mt-4 hidden md:flex w-full items-center justify-center p-2 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors"
            aria-label="Expand sidebar"
            title="Expand sidebar"
          >
            <ChevronRight className="w-5 h-5" />
          </button>
        )}
      </div>

      {/* Recording indicator in nav. On the collapsed desktop rail this
          shrinks to just the pulsing dot (the live timer + label are
          hidden); the elapsed time stays available via the title
          tooltip and is fully visible again on expand or on mobile. */}
      {isRecording && (
        <div className={`mb-4 py-2 bg-red-600/20 border border-red-500/30 rounded-lg ${collapsed ? 'md:px-0 px-3' : 'px-3'}`}>
          <a
            href="#/record"
            onClick={onClose}
            title={collapsed ? `Recording — ${formatTime(recordingTime)}` : undefined}
            className={`flex items-center gap-2 text-red-400 hover:text-red-300 transition-colors ${collapsed ? 'md:justify-center' : ''}`}
          >
            <Circle className="w-3 h-3 fill-red-400 animate-pulse shrink-0" />
            <span className={`text-sm font-medium ${collapsed ? 'md:hidden' : ''}`}>Recording</span>
            <span className={`ml-auto text-xs font-mono flex items-center gap-1 ${collapsed ? 'md:hidden' : ''}`}>
              <Clock className="w-3 h-3" />
              {formatTime(recordingTime)}
            </span>
          </a>
        </div>
      )}

      <div className="space-y-2">
        {navItems.map(renderNavItem)}
      </div>

      {/* Admin-only section. Renders only when the current user is an
          admin in the active organization (or a global superuser). The
          subtle divider + section heading visually separates these
          items per Aaron's "labeled as such" directive. Non-admin
          users see exactly the workspace nav with no admin signal. */}
      {isAdmin && (
        <div className="mt-6">
          <div className="mt-2 border-t border-zinc-800/70" />
          <div className={`mt-4 mb-2 flex items-center gap-2 px-3 ${collapsed ? 'md:justify-center md:px-0' : ''}`}>
            <ShieldCheck className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
            <span className={`text-[11px] uppercase tracking-wider text-zinc-500 ${collapsed ? 'md:hidden' : ''}`}>{isEnterpriseWorkspace ? 'Admin' : 'Workspace'}</span>
          </div>
          <div className="space-y-2">
            {adminNavItems.map(renderNavItem)}
          </div>
        </div>
      )}

      <div className={`absolute bottom-4 left-4 right-4 ${collapsed ? 'md:left-2 md:right-2' : ''}`}>
        <button
          onClick={() => { logout(); onClose(); }}
          title={collapsed ? 'Logout' : undefined}
          aria-label="Logout"
          className={`w-full flex items-center justify-center gap-2 px-3 py-2 bg-zinc-900 hover:bg-zinc-800 text-zinc-400 hover:text-white rounded-lg transition-colors text-sm min-h-[44px] ${
            collapsed ? 'md:px-0' : ''
          }`}
        >
          <LogOut className={`w-4 h-4 shrink-0 ${collapsed ? 'md:block hidden' : 'hidden'}`} />
          <span className={collapsed ? 'md:hidden' : ''}>Logout</span>
        </button>
      </div>
    </nav>
  );
}

// Protected Route Component
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const { hasAccess, isLoading: isOrgLoading } = useOrg();

  // Wait for SSO probe before deciding
  if (isLoading || isOrgLoading) {
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">Authenticating…</div></div>;
  }

  // Not authed → kick to oauth2-proxy SSO sign-in (Keycloak)
  if (!isAuthenticated) {
    redirectToLogin();
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">Redirecting to sign-in…</div></div>;
  }

  if (!hasAccess) {
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">No organization access.</div></div>;
  }

  return (
    <AppLayout>
      <ErrorBoundary scope="page" resetKey={window.location.hash}>{children}</ErrorBoundary>
    </AppLayout>
  );
}

/**
 * Admin-only wrapper. Same gating as the sidebar — if the user isn't
 * an admin in the active org (and not a global superuser), bounce
 * them to /dashboard. Prevents direct-URL access to /rooms,
 * /speakers, and /admin/* for regular users. Pages still apply their
 * own internal admin checks for finer-grained controls (e.g. only
 * enroll speakers as admin), but this is the route-level gate.
 */
function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isAuthenticated, isLoading } = useAuth();
  const { activeOrganization, hasAccess, isLoading: isOrgLoading } = useOrg();

  if (isLoading || isOrgLoading) {
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">Authenticating…</div></div>;
  }
  if (!isAuthenticated) {
    redirectToLogin();
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">Redirecting to sign-in…</div></div>;
  }
  if (!hasAccess) {
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">No organization access.</div></div>;
  }
  if (!isAdminRole(activeOrganization?.role, user?.is_superuser)) {
    // Silently bounce, no flash of forbidden content.
    return <Navigate to="/dashboard" replace />;
  }
  return (
    <AppLayout>
      <ErrorBoundary scope="page" resetKey={window.location.hash}>{children}</ErrorBoundary>
    </AppLayout>
  );
}

/**
 * `/` route. Three behaviors gated by build-time
 * `VITE_LANDING_PAGE_ENABLED` and runtime auth state:
 *   - Authenticated user → /dashboard (always).
 *   - Anonymous + flag === "true" → Landing page (public, no API call).
 *   - Anonymous + flag !== "true" → /dashboard (historical dev behavior;
 *     ProtectedRoute will bounce to oauth2-proxy sign-in).
 */
function RootRoute({ isAuthenticated }: { isAuthenticated: boolean }) {
  if (isAuthenticated) {
    // Record-first: authenticated users land on the recorder.
    return <Navigate to="/record" replace />;
  }
  const landingEnabled =
    String(import.meta.env.VITE_LANDING_PAGE_ENABLED || '').toLowerCase() === 'true';
  if (landingEnabled) {
    return <Landing />;
  }
  return <Navigate to="/dashboard" replace />;
}

// Main Router
export function AppRouterSimplified() {
  const { isAuthenticated, isLoading } = useAuth();
  const { isLoading: isOrgLoading } = useOrg();

  if (isLoading || isOrgLoading) {
    return <div className="min-h-screen flex items-center justify-center bg-gray-50"><div className="text-gray-500">Authenticating…</div></div>;
  }

  return (
    <Suspense fallback={<RouteLoadingFallback />}>
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/signup" element={<Signup />} />
      <Route path="/pricing" element={<Pricing />} />
      {/* v3.21.0 password-reset flow. Both routes are unauthenticated by
          design — a logged-out user with a stale session needs to be
          able to land here. */}
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      {/* v3.21.0 Founding 100 cohort landing page. Public, server-cached
          counter via /api/founding/status. */}
      <Route path="/founding-100" element={<Founding100 />} />
      {/* v3.21.0 /contact is public — renders for authed AND unauthed
          visitors. The page itself reads useAuth() and adjusts copy +
          nav back-link accordingly. Submitting posts to
          /api/support/contact which accepts both. */}
      <Route path="/contact" element={<Contact />} />

      {/* v3.22.5 legal pages. All public, unauthenticated. Anonymous
          visitors must be able to read these without an account.
          See frontend/src/pages/{Terms,Privacy,AUP}.tsx. */}
      <Route path="/terms" element={<Terms />} />
      <Route path="/privacy" element={<Privacy />} />
      <Route path="/aup" element={<AUP />} />

      {/*
        Public invite-only landing at `/`. Behavior is build-time gated
        by VITE_LANDING_PAGE_ENABLED so bigboy dev (the historical
        `/` → `/dashboard` redirect) keeps working; the VPS prod build
        at meeting-ops.unicorncommander.ai sets the flag to `true` and
        gets the Landing page for anonymous visitors. Authenticated
        users always land on /dashboard regardless of the flag.
       */}
      <Route path="/" element={<RootRoute isAuthenticated={isAuthenticated} />} />

      <Route path="/dashboard" element={
        <ProtectedRoute>
          <Dashboard />
        </ProtectedRoute>
      } />

      {/*
        /record is preserved as a redirect to /record/personal so any
        existing bookmarks, recording-indicator hash links, and
        notification surfaces keep working. The personal/browser
        recording page lives under /record/personal; future room-host
        UIs can land under /record/<roomid>.
       */}
      <Route path="/record" element={<Navigate to="/record/personal" replace />} />
      <Route path="/record/personal" element={
        <ProtectedRoute>
          <LiveRecording />
        </ProtectedRoute>
      } />

      <Route path="/sessions" element={
        <ProtectedRoute>
          <Sessions />
        </ProtectedRoute>
      } />
      <Route path="/action-items" element={
        <ProtectedRoute>
          <ActionItems />
        </ProtectedRoute>
      } />
      <Route path="/decisions-risks" element={
        <ProtectedRoute>
          <DecisionsRisks />
        </ProtectedRoute>
      } />
      <Route path="/lectures" element={
        <ProtectedRoute>
          <Lectures />
        </ProtectedRoute>
      } />
      <Route path="/moments" element={
        <ProtectedRoute>
          <ImportantMoments />
        </ProtectedRoute>
      } />
      <Route path="/import" element={
        <ProtectedRoute>
          <Import />
        </ProtectedRoute>
      } />

      <Route path="/sessions/:id" element={
        <ProtectedRoute>
          <SessionDetails />
        </ProtectedRoute>
      } />

      {/* Privacy-mode session surface — IndexedDB-only, no server
          calls. Auth-gated like the other user routes so an
          unauthenticated tab redirects to OIDC sign-in first; the
          content itself isn't server-scoped because it never left
          the device. */}
      <Route path="/local-sessions" element={
        <ProtectedRoute>
          <LocalSessions />
        </ProtectedRoute>
      } />
      <Route path="/local-sessions/:id" element={
        <ProtectedRoute>
          <LocalSessions />
        </ProtectedRoute>
      } />

      {/* Magic-link landing: post-login redemption -> navigates to /sessions/:id */}
      <Route path="/shared/sessions" element={
        <ProtectedRoute>
          <SharedSessionRedirect />
        </ProtectedRoute>
      } />

      {/* Admin-gated surfaces. Speakers + Rooms + Agents are all
          workspace-configuration features Aaron flagged as "admin
          stuff"; route-level guard bounces non-admin direct links to
          /dashboard so the sidebar gating + URL gating stay in sync. */}
      <Route path="/speakers" element={
        <AdminRoute>
          <SpeakerLibrary />
        </AdminRoute>
      } />

      <Route path="/rooms" element={
        <AdminRoute>
          <Rooms />
        </AdminRoute>
      } />

      <Route path="/rooms/new" element={
        <AdminRoute>
          <RoomSetupWizard />
        </AdminRoute>
      } />

      <Route path="/rooms/:id" element={
        <AdminRoute>
          <RoomDetail />
        </AdminRoute>
      } />

      <Route path="/settings" element={
        <ProtectedRoute>
          <SettingsEnhanced />
        </ProtectedRoute>
      } />

      <Route path="/admin/agents" element={
        <AdminRoute>
          <AgentDashboard />
        </AdminRoute>
      } />

      <Route path="/admin/bulk-import" element={
        <AdminRoute>
          <AdminBulkImport />
        </AdminRoute>
      } />

      {/* Launch console: comps + invite-code oversight. AdminRoute lets an
          org-admin through, but the page itself + its backend are
          platform-superuser only, so a non-superuser sees an access notice. */}
      <Route path="/admin/comps" element={
        <AdminRoute>
          <CompsAdmin />
        </AdminRoute>
      } />

            <Route path="/rag" element={
        <ProtectedRoute>
          <RAGChat />
        </ProtectedRoute>
      } />

      <Route path="/digests" element={
        <ProtectedRoute>
          <DigestsPage />
        </ProtectedRoute>
      } />

      <Route path="/knowledge-graph" element={
        <ProtectedRoute>
          <KnowledgeGraph />
        </ProtectedRoute>
      } />

      <Route path="/help" element={
        <ProtectedRoute>
          <Help />
        </ProtectedRoute>
      } />

      {/* Phase B.1 server-live streaming smoke test. Admin-only — exists to
          prove the Cloudflare -> oauth2-proxy -> Traefik -> FastAPI WS
          upgrade seam ahead of B.2 real-model integration. See
          docs/phase-b-server-live-streaming.md. */}
      <Route path="/streaming-test" element={
        <AdminRoute>
          <StreamingTest />
        </AdminRoute>
      } />

      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
    </Suspense>
  );
}
