/**
 * Centralized role-check helpers for org-scoped admin gating.
 *
 * isAdminRole is true when:
 * - the user is a global superuser (is_superuser flag on User), OR
 * - the user's role in the active org is 'admin' or 'superuser'.
 *
 * Use this for sidebar visibility, AdminRoute gating, Settings tab filtering.
 * The active org's role is checked (not user.role) so admin-in-org-A / member-in-org-B
 * shows admin UI only when org A is selected.
 */
export function isAdminRole(role?: string, isSuperuser?: boolean): boolean {
  if (isSuperuser) return true;
  return role === 'admin' || role === 'superuser';
}
