import type { User } from '@/types'

/**
 * Whether the signed-in user may create projects. Admins always may; anyone
 * else unless the server turned `can_create_projects` off for them (for
 * example SSO users outside OIDC_PROJECT_CREATOR_GROUPS). The API enforces the
 * same rule, so this only decides whether "New Project" is offered.
 */
export function canCreateProjects(user: User | null | undefined): boolean {
  if (!user) return false
  return user.is_superadmin || user.can_create_projects !== false
}
