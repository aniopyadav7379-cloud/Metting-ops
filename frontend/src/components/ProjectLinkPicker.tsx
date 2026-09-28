import React, { useEffect, useState, useCallback } from 'react';
import { Briefcase, Loader2, X } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';

export interface ProjectLink {
  project_app: string | null;   // 'project-ops' | 'crisis-ops' | null
  // project_id is a string. For project-ops it is a UUID; for crisis-ops it
  // will be whatever shape that app exposes. Backed by a TEXT column on
  // recording_sessions (migrated 010_project_id_to_text).
  project_id: string | null;
  project_slug: string | null;
}

interface Project {
  id: string;
  name: string;
  slug: string;
  app: string;
  // Optional richer fields surfaced from project-ops so the dropdown can
  // show humans-friendly labels: "Q2 Roadmap (PROJ-2026-000067)".
  project_number?: string | null;
  status?: string | null;
  priority?: string | null;
  client_name?: string | null;
  due_date?: string | null;
}

interface ProjectLinkPickerProps {
  /** Current project link value (controlled). */
  value: ProjectLink;
  /** Called whenever the user changes app or project selection. */
  onChange: (link: ProjectLink) => void;
  /** Optional className passed to the outer container. */
  className?: string;
  /** Optional label override (default: "Link to project"). */
  label?: string;
  /** Hide the section heading entirely (used for inline edit modes). */
  hideLabel?: boolean;
}

const APP_OPTIONS = [
  { value: '', label: 'No project link' },
  { value: 'project-ops', label: 'Project-Ops' },
  { value: 'crisis-ops', label: 'Crisis-Ops' },
];

/**
 * Reusable project picker. Used by both SessionCreator and SessionDetails.
 *
 * Behavior:
 *  - User picks an app → fetches GET /api/integrations/{app}/projects
 *    using the active org from useOrg().
 *  - On any error or empty result, the project dropdown shows
 *    "No projects available" and the picker effectively no-ops
 *    (parent still gets onChange({ all-null }) so create/save proceeds
 *    without a link).
 *  - All requests rely on the cookie-based SSO flow; the AuthContext
 *    fetch interceptor already attaches the X-MeetingOps-Org header
 *    and any bearer token.
 */
export const ProjectLinkPicker: React.FC<ProjectLinkPickerProps> = ({
  value,
  onChange,
  className = '',
  label = 'Link to project',
  hideLabel = false,
}) => {
  const { activeOrganization } = useOrg();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedApp = value.project_app ?? '';
  const selectedProjectId = value.project_id ?? null;

  const fetchProjects = useCallback(
    async (app: string) => {
      if (!app || !activeOrganization) {
        setProjects([]);
        setError(null);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        // Relative URL so the request goes through the same oauth2-proxy
        // session cookie as /api/auth/me. Using config.apiBaseUrl can land
        // on a stale subdomain (api.{root}) that does not exist for the
        // multi-tenant cloud build, which manifests as "Project list
        // unavailable".
        const url =
          `/api/integrations/${encodeURIComponent(app)}/projects` +
          `?organization_id=${activeOrganization.id}`;
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (activeOrganization.slug) {
          headers['X-MeetingOps-Org'] = activeOrganization.slug;
        }
        const res = await fetch(url, { credentials: 'same-origin', headers });
        if (!res.ok) {
          // Graceful no-op: clear list, surface a tiny inline message
          // eslint-disable-next-line no-console
          console.warn(`[ProjectLinkPicker] fetch HTTP ${res.status} for ${url}`);
          setProjects([]);
          setError(`No projects available (${res.status})`);
          return;
        }
        const data = (await res.json()) as Project[];
        setProjects(Array.isArray(data) ? data : []);
        if (!Array.isArray(data) || data.length === 0) {
          setError('No projects available');
        }
      } catch (err) {
        // Network error → silent no-op with inline note; do not block the form
        setProjects([]);
        setError('Project list unavailable');
        // eslint-disable-next-line no-console
        console.warn('[ProjectLinkPicker] fetch failed:', err);
      } finally {
        setLoading(false);
      }
    },
    [activeOrganization],
  );

  useEffect(() => {
    fetchProjects(selectedApp);
  }, [selectedApp, fetchProjects]);

  const handleAppChange = (app: string) => {
    if (!app) {
      onChange({ project_app: null, project_id: null, project_slug: null });
      return;
    }
    // Clear project selection when switching apps
    onChange({ project_app: app, project_id: null, project_slug: null });
  };

  const handleProjectChange = (projectIdRaw: string) => {
    if (!projectIdRaw) {
      onChange({
        project_app: value.project_app,
        project_id: null,
        project_slug: null,
      });
      return;
    }
    const proj = projects.find((p) => p.id === projectIdRaw);
    onChange({
      project_app: value.project_app,
      project_id: projectIdRaw,
      project_slug: proj?.slug ?? null,
    });
  };

  const clearLink = () => {
    onChange({ project_app: null, project_id: null, project_slug: null });
  };

  return (
    <div className={className}>
      {!hideLabel && (
        <div className="flex items-center justify-between mb-3">
          <label className="text-sm font-medium text-gray-300 flex items-center gap-2">
            <Briefcase className="w-4 h-4 text-purple-400" />
            {label}
            <span className="text-xs text-gray-500 font-normal">(optional)</span>
          </label>
          {selectedApp && (
            <button
              type="button"
              onClick={clearLink}
              className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200"
              title="Remove project link"
            >
              <X className="w-3 h-3" />
              Clear
            </button>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <select
          value={selectedApp}
          onChange={(e) => handleAppChange(e.target.value)}
          className="px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white text-sm focus:outline-none focus:border-purple-500"
        >
          {APP_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <div className="relative">
          <select
            value={selectedProjectId ?? ''}
            onChange={(e) => handleProjectChange(e.target.value)}
            disabled={!selectedApp || loading || projects.length === 0}
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white text-sm focus:outline-none focus:border-purple-500 disabled:opacity-50"
          >
            {!selectedApp ? (
              <option value="">Pick an app first</option>
            ) : loading ? (
              <option value="">Loading projects...</option>
            ) : projects.length === 0 ? (
              <option value="">{error || 'No projects available'}</option>
            ) : (
              <>
                <option value="">— Select a project —</option>
                {projects.map((p) => {
                  // Build a richer label so users can disambiguate by
                  // project number and client when titles repeat.
                  const parts = [p.name];
                  if (p.project_number) parts.push(`(${p.project_number})`);
                  if (p.client_name) parts.push(`— ${p.client_name}`);
                  return (
                    <option key={p.id} value={p.id}>
                      {parts.join(' ')}
                    </option>
                  );
                })}
              </>
            )}
          </select>
          {loading && (
            <Loader2 className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-purple-400 animate-spin pointer-events-none" />
          )}
        </div>
      </div>

      {selectedApp && error && projects.length === 0 && !loading && (
        <p className="mt-2 text-xs text-gray-500">
          {error}. The link will be skipped.
        </p>
      )}
    </div>
  );
};

export default ProjectLinkPicker;
