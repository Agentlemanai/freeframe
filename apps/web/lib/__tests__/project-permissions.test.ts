import { describe, expect, it } from 'vitest'
import { canCreateProjects } from '../project-permissions'
import type { User } from '@/types'

const user = (over: Partial<User>): User =>
  ({ id: '1', email: 'a@b.c', name: 'A', avatar_url: null, avatar_color: null, status: 'active',
     is_superadmin: false, email_verified: true, preferences: {}, created_at: '', deleted_at: null, ...over }) as User

describe('canCreateProjects', () => {
  it('allows everyone by default, including older servers without the field', () => {
    expect(canCreateProjects(user({}))).toBe(true)
    expect(canCreateProjects(user({ can_create_projects: true }))).toBe(true)
  })
  it('hides it when the server turned it off', () => {
    expect(canCreateProjects(user({ can_create_projects: false }))).toBe(false)
  })
  it('always allows admins', () => {
    expect(canCreateProjects(user({ is_superadmin: true, can_create_projects: false }))).toBe(true)
  })
  it('is false without a user', () => {
    expect(canCreateProjects(null)).toBe(false)
  })
})
