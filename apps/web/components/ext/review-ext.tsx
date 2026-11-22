'use client'

/**
 * Garlomedia fork: review tasks and client / internal visibility on the review
 * screen. Everything the fork adds to the web app lives in components/ext; the
 * upstream files only mount these components (see docs/ext-review-tasks.md).
 *
 * The server decides everything shown here. This context only carries what
 * `GET /ext/assets/{id}/context` and `GET /ext/tasks` answered, so that the
 * review screen offers a person what they are allowed to do.
 */

import * as React from 'react'
import useSWR, { mutate as globalMutate } from 'swr'
import { api } from '@/lib/api'

export type CommentVisibility = 'public' | 'internal'

export interface ReviewContextInfo {
  sees_internal: boolean
  is_manager: boolean
  default_comment_visibility: CommentVisibility
  can_choose_comment_visibility: boolean
}

export const TASK_KINDS = [
  ['cut_trim', 'Cut / trim'],
  ['b_roll', 'B-roll'],
  ['music', 'Music'],
  ['sfx', 'Sound effect'],
  ['caption_title', 'Caption / title'],
  ['mograph', 'Motion graphics'],
  ['audio', 'Audio'],
  ['colour', 'Colour'],
  ['question', 'Question'],
  ['global', 'Whole video'],
] as const

export const TASK_STAGES = [
  ['selects', 'Selects'],
  ['radio', 'Radio edit'],
  ['rough', 'Rough cut'],
  ['fine', 'Fine cut'],
  ['finish', 'Finish'],
  ['review', 'Client review'],
  ['deliver', 'Deliver'],
] as const

/** Stages an agent may be assigned at. The server enforces this; the form only
 *  avoids offering what would be refused. */
export const AGENT_STAGES: readonly string[] = ['radio', 'rough', 'review']

export type TaskStatus =
  | 'open' | 'claimed' | 'in_progress' | 'proposed' | 'needs_info' | 'approved' | 'cancelled'

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  open: 'Open',
  claimed: 'Claimed',
  in_progress: 'In progress',
  proposed: 'Waiting for approval',
  needs_info: 'Needs an answer',
  approved: 'Approved',
  cancelled: 'Cancelled',
}

export interface ReviewTask {
  id: string
  project_id: string
  asset_id: string
  created_on_version_id: string | null
  frame_in: number | null
  frame_out: number | null
  kind: string
  instruction: string
  stage: string
  assignee_type: 'person' | 'role_pool' | 'agent'
  assignee_person_id: string | null
  assignee_role: string | null
  status: TaskStatus
  source_comment_id: string | null
  result_version_id: string | null
  claimed_by: string | null
  created_at: string
}

export interface ReviewTaskNote {
  id: string
  task_id: string
  kind: string
  body: string
  author_type: 'person' | 'agent'
  author_person_id: string | null
  author_label: string | null
  created_at: string
}

export interface TeamMember {
  id: string
  name: string
}

interface ReviewExtValue {
  assetId: string
  projectId: string
  info: ReviewContextInfo
  tasks: ReviewTask[]
  team: TeamMember[]
  refreshTasks: () => Promise<unknown>
  /** The comment a "Make task" form is open for, if any. */
  makeTaskFor: { id: string; body: string } | null
  openMakeTask: (comment: { id: string; body: string } | null) => void
}

const ReviewExtContext = React.createContext<ReviewExtValue | null>(null)

/** Null outside the review screen (share pages, the project page), where every
 *  ext component renders nothing. */
export function useReviewExt(): ReviewExtValue | null {
  return React.useContext(ReviewExtContext)
}

export const tasksKey = (assetId: string) => `/ext/tasks?asset_id=${assetId}`
export const commentsKey = (assetId: string, versionId: string) =>
  `/assets/${assetId}/comments?version_id=${versionId}`

async function fetchTeam(projectId: string): Promise<TeamMember[]> {
  const members = await api.get<Array<{ user_id: string; role: string }>>(`/projects/${projectId}/members`)
  const team = (members ?? []).filter((m) => m.role === 'owner' || m.role === 'editor')
  if (team.length === 0) return []
  const users = await api.get<TeamMember[]>(`/users?ids=${team.map((m) => m.user_id).join(',')}`)
  return (users ?? []).map((u) => ({ id: u.id, name: u.name }))
}

export function ReviewExtProvider({
  assetId,
  projectId,
  children,
}: {
  assetId?: string | null
  projectId?: string | null
  children: React.ReactNode
}) {
  // A server without the extension answers 404; the screen then behaves as upstream's.
  const { data: info } = useSWR<ReviewContextInfo>(
    assetId ? `/ext/assets/${assetId}/context` : null,
    (key: string) => api.get<ReviewContextInfo>(key),
    { revalidateOnFocus: false, shouldRetryOnError: false },
  )
  const team = info?.sees_internal === true
  const { data: tasks, mutate } = useSWR<ReviewTask[]>(
    team && assetId ? tasksKey(assetId) : null,
    (key: string) => api.get<ReviewTask[]>(key),
    { revalidateOnFocus: false },
  )
  const { data: members } = useSWR<TeamMember[]>(
    team && projectId ? `ext-team:${projectId}` : null,
    () => fetchTeam(projectId as string),
    { revalidateOnFocus: false },
  )
  const [makeTaskFor, openMakeTask] = React.useState<{ id: string; body: string } | null>(null)

  const value = React.useMemo<ReviewExtValue | null>(
    () =>
      info && assetId && projectId
        ? {
            assetId,
            projectId,
            info,
            tasks: tasks ?? [],
            team: members ?? [],
            refreshTasks: () => mutate(),
            makeTaskFor,
            openMakeTask,
          }
        : null,
    [info, assetId, projectId, tasks, members, mutate, makeTaskFor],
  )
  return <ReviewExtContext.Provider value={value}>{children}</ReviewExtContext.Provider>
}

/** What the review screen does when the server says something of the fork's changed. */
export function revalidateForExtEvent(
  type: string,
  assetId: string | undefined,
  versionId: string | undefined,
  refetchVersions: () => unknown,
): void {
  if (!assetId) return
  if (type === 'review_tasks_changed') void globalMutate(tasksKey(assetId))
  if (type === 'version_published') void refetchVersions()
  if (type === 'comment_visibility_changed' && versionId) void globalMutate(commentsKey(assetId, versionId))
}

export function assigneeLabel(task: ReviewTask, team: TeamMember[]): string {
  if (task.assignee_type === 'agent') return 'AI agent'
  if (task.assignee_type === 'role_pool') return `Any ${task.assignee_role ?? 'editor'}`
  return team.find((m) => m.id === task.assignee_person_id)?.name ?? 'Someone'
}

export function kindLabel(kind: string): string {
  return TASK_KINDS.find(([value]) => value === kind)?.[1] ?? kind
}

export function stageLabel(stage: string): string {
  return TASK_STAGES.find(([value]) => value === stage)?.[1] ?? stage
}
