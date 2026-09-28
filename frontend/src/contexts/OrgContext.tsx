import React, { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { useAuth } from './AuthContext';
import {
  appendOrgQuery,
  clearStoredOrgSlug,
  DEFAULT_ORG_SLUG,
  getStoredOrgSlug,
  setDefaultOrganization,
  setStoredOrgSlug,
  type OrganizationMembership,
} from '../utils/organization';

interface OrgContextType {
  organizations: OrganizationMembership[];
  activeOrganization: OrganizationMembership | null;
  isLoading: boolean;
  hasAccess: boolean;
  switchOrganization: (slug: string) => void;
  getOrgQueryUrl: (url: string) => string;
}

const OrgContext = createContext<OrgContextType | undefined>(undefined);

function resolveActiveOrganization(
  organizations: OrganizationMembership[],
  serverDefaultOrgId?: number | null,
): OrganizationMembership | null {
  if (organizations.length === 0) {
    return null;
  }

  // 1. Server-side "home"/default workspace — the cross-device truth set via
  //    PUT /api/organizations/default. Preferred ahead of the localStorage
  //    sticky so a user's chosen workspace follows them onto a new device,
  //    matching the backend's resolve_active_organization preference. Only
  //    honored when they're actually a member.
  if (serverDefaultOrgId != null) {
    const serverMatch = organizations.find((org) => org.id === serverDefaultOrgId);
    if (serverMatch) {
      return serverMatch;
    }
  }

  // 2. localStorage sticky — fast-path/offline fallback (the user's most
  //    recent explicit choice on THIS device).
  const storedSlug = getStoredOrgSlug();
  if (storedSlug) {
    const storedMatch = organizations.find((org) => org.slug === storedSlug);
    if (storedMatch) {
      return storedMatch;
    }
  }

  if (organizations.length === 1) {
    return organizations[0];
  }

  // 3. Platform "home" org, else the first membership.
  const defaultOrg = organizations.find((org) => org.slug === DEFAULT_ORG_SLUG);
  return defaultOrg ?? organizations[0];
}

export function OrgProvider({ children }: { children: ReactNode }) {
  const { user, isAuthenticated, isLoading: authLoading } = useAuth();
  const organizations = useMemo(
    () => user?.organizations ?? [],
    [user],
  );
  const serverDefaultOrgId = user?.default_organization_id ?? null;
  const [activeOrganization, setActiveOrganization] = useState<OrganizationMembership | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (authLoading) {
      setIsLoading(true);
      return;
    }

    if (!isAuthenticated) {
      setActiveOrganization(null);
      clearStoredOrgSlug();
      setIsLoading(false);
      return;
    }

    const nextOrg = resolveActiveOrganization(organizations, serverDefaultOrgId);
    setActiveOrganization(nextOrg);
    if (nextOrg) {
      setStoredOrgSlug(nextOrg.slug);
    } else {
      clearStoredOrgSlug();
    }
    setIsLoading(false);
  }, [authLoading, isAuthenticated, organizations, serverDefaultOrgId]);

  const switchOrganization = (slug: string) => {
    const nextOrg = organizations.find((organization) => organization.slug === slug);
    if (!nextOrg) {
      return;
    }

    setActiveOrganization(nextOrg);
    setStoredOrgSlug(nextOrg.slug);
    // Persist the choice server-side so it follows the user across devices +
    // nodes (best-effort, non-blocking — the localStorage sticky above is the
    // offline source of truth if this fails).
    void setDefaultOrganization(nextOrg.id);
  };

  const value = useMemo<OrgContextType>(() => ({
    organizations,
    activeOrganization,
    isLoading,
    hasAccess: !isAuthenticated || organizations.length > 0,
    switchOrganization,
    getOrgQueryUrl: (url: string) => appendOrgQuery(url, activeOrganization?.slug),
  }), [organizations, activeOrganization, isLoading, isAuthenticated]);

  return (
    <OrgContext.Provider value={value}>
      {children}
    </OrgContext.Provider>
  );
}

export function useOrg() {
  const context = useContext(OrgContext);
  if (!context) {
    throw new Error('useOrg must be used within an OrgProvider');
  }
  return context;
}
