import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { useReviewStore } from '@/stores/review-store'

// What the server would answer, per test. The components show a person only
// what `GET /ext/assets/{id}/context` says they may do; the server enforces it.
const server = vi.hoisted(() => ({
  context: null as Record<string, unknown> | null,
  tasks: [] as Record<string, unknown>[],
  posts: [] as Array<[string, unknown]>,
  patches: [] as Array<[string, unknown]>,
}))

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(async (url: string) => {
      if (url.endsWith('/context')) {
        if (!server.context) throw Object.assign(new Error('Not Found'), { status: 404 })
        return server.context
      }
      if (url.startsWith('/ext/tasks?')) return server.tasks
      if (url.endsWith('/notes')) return []
      if (url.endsWith('/members')) return [{ user_id: 'u-ed', role: 'editor' }, { user_id: 'u-cl', role: 'reviewer' }]
      if (url.startsWith('/users?')) return [{ id: 'u-ed', name: 'Eddie Editor' }]
      return []
    }),
    post: vi.fn(async (url: string, body: unknown) => {
      server.posts.push([url, body])
      return {}
    }),
    patch: vi.fn(async (url: string, body: unknown) => {
      server.patches.push([url, body])
      return {}
    }),
  },
}))
vi.mock('@/components/shared/toast', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
}))
vi.mock('@/components/review/review-provider', () => ({
  useReview: () => ({ pauseVideo: vi.fn(), registerPauseHandler: vi.fn() }),
}))

import { ReviewExtProvider } from '../review-ext'
import { CommentExtActions, MakeTaskDialog, ReviewTaskPanel, VersionVisibility } from '../review-tasks'
import { CommentInput } from '@/components/review/comment-input'

const MANAGER = { sees_internal: true, is_manager: true, default_comment_visibility: 'internal', can_choose_comment_visibility: true }
const EDITOR = { sees_internal: true, is_manager: false, default_comment_visibility: 'internal', can_choose_comment_visibility: false }
const CLIENT = { sees_internal: false, is_manager: false, default_comment_visibility: 'public', can_choose_comment_visibility: false }

const task = (over: Record<string, unknown> = {}) => ({
  id: 't1', project_id: 'p1', asset_id: 'a1', created_on_version_id: 'v1', frame_in: 50, frame_out: 50,
  kind: 'cut_trim', instruction: 'Trim the pause', stage: 'rough', assignee_type: 'role_pool',
  assignee_person_id: null, assignee_role: 'editor', status: 'open', source_comment_id: null,
  result_version_id: null, claimed_by: null, created_at: '2026-11-22T10:00:00Z', ...over,
})

const versions = [
  { id: 'v1', asset_id: 'a1', version_number: 1, processing_status: 'ready', visibility: 'public' },
  { id: 'v2', asset_id: 'a1', version_number: 2, processing_status: 'ready', visibility: 'internal' },
] as never[]

function screenFor(children: React.ReactNode) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <ReviewExtProvider assetId="a1" projectId="p1">{children}</ReviewExtProvider>
    </SWRConfig>,
  )
}

beforeEach(() => {
  server.context = null
  server.tasks = []
  server.posts = []
  server.patches = []
  useReviewStore.getState().reset()
})

describe('on a comment', () => {
  const comment = { id: 'c1', body: 'Too long at the start', visibility: 'public', version_id: 'v1' }

  it('offers a client nothing', async () => {
    server.context = CLIENT
    screenFor(<><CommentExtActions comment={comment} /><span>loaded</span></>)
    await screen.findByText('loaded')
    await waitFor(() => expect(screen.queryByRole('button')).toBeNull())
  })

  it('renders nothing against a server without the extension, or outside the review screen', async () => {
    screenFor(<><CommentExtActions comment={comment} /><span>loaded</span></>)
    await screen.findByText('loaded')
    expect(screen.queryByRole('button')).toBeNull()
    render(<CommentExtActions comment={comment} />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('offers an editor Make task but not who-sees-it', async () => {
    server.context = EDITOR
    screenFor(<CommentExtActions comment={comment} />)
    expect(await screen.findByRole('button', { name: 'Make task' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Make internal' })).toBeNull()
  })

  it('lets a manager hide a comment from the client and show an internal one', async () => {
    server.context = MANAGER
    const { unmount } = screenFor(<CommentExtActions comment={comment} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Make internal' }))
    await waitFor(() => expect(server.patches).toEqual([['/ext/comments/c1/visibility', { visibility: 'internal' }]]))
    unmount()
    screenFor(<CommentExtActions comment={{ ...comment, visibility: 'internal' }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Show to client' }))
    await waitFor(() => expect(server.patches[1]).toEqual(['/ext/comments/c1/visibility', { visibility: 'public' }]))
  })

  it('shows the status of the task a comment already became, instead of Make task', async () => {
    server.context = EDITOR
    server.tasks = [task({ source_comment_id: 'c1', status: 'in_progress' })]
    screenFor(<CommentExtActions comment={comment} />)
    expect(await screen.findByText('In progress')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Make task' })).toBeNull()
  })
})

describe('Make task', () => {
  it('creates a task from the comment, and does not offer an agent at a people-only stage', async () => {
    server.context = MANAGER
    screenFor(<><CommentExtActions comment={{ id: 'c1', body: 'Too long at the start' }} /><MakeTaskDialog /></>)
    fireEvent.click(await screen.findByRole('button', { name: 'Make task' }))
    expect(await screen.findByLabelText('What needs changing')).toHaveValue('Too long at the start')

    const agent = () => screen.getByRole('option', { name: /AI agent/ }) as HTMLOptionElement
    expect(agent().disabled).toBe(false)                       // the default stage, client review, allows one
    fireEvent.change(screen.getByLabelText('Who does it'), { target: { value: 'agent' } })
    fireEvent.change(screen.getByLabelText('Stage'), { target: { value: 'fine' } })
    await waitFor(() => expect(agent().disabled).toBe(true))
    expect(screen.getByLabelText('Who does it')).toHaveValue('pool')   // moved off the agent

    await screen.findByRole('option', { name: 'Eddie Editor' })
    expect(screen.queryByRole('option', { name: /reviewer/i })).toBeNull()
    fireEvent.change(screen.getByLabelText('Who does it'), { target: { value: 'u-ed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }))
    await waitFor(() => expect(server.posts).toEqual([['/ext/tasks', {
      asset_id: 'a1', source_comment_id: 'c1', instruction: 'Too long at the start',
      kind: 'cut_trim', stage: 'fine', assignee_type: 'person', assignee_person_id: 'u-ed',
    }]]))
  })
})

describe('the task panel', () => {
  it('is absent when there are no tasks, and for a client', async () => {
    server.context = MANAGER
    const first = screenFor(<><ReviewTaskPanel versions={versions} /><span>loaded</span></>)
    await screen.findByText('loaded')
    expect(screen.queryByText('Tasks')).toBeNull()
    first.unmount()
    server.context = CLIENT
    server.tasks = [task()]
    screenFor(<><ReviewTaskPanel versions={versions} /><span>loaded</span></>)
    await screen.findByText('loaded')
    await waitFor(() => expect(screen.queryByText('Tasks')).toBeNull())
  })

  it('says what is waiting, and lets a manager approve a proposed task in one click', async () => {
    server.context = MANAGER
    server.tasks = [task(), task({ id: 't2', status: 'proposed', result_version_id: 'v2', instruction: 'Swap the music' })]
    screenFor(<ReviewTaskPanel versions={versions} />)
    fireEvent.click(await screen.findByRole('button', { name: /Tasks/ }))
    expect(screen.getByText('2 active · 1 for you to approve')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(server.posts).toEqual([['/ext/tasks/t2/approve', {}]]))
  })

  it('gives an editor no approve or reject', async () => {
    server.context = EDITOR
    server.tasks = [task({ status: 'proposed', result_version_id: 'v2' })]
    screenFor(<ReviewTaskPanel versions={versions} />)
    fireEvent.click(await screen.findByRole('button', { name: /Tasks/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Show task details' }))
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reject…' })).toBeNull()
    expect(screen.getByRole('button', { name: /Add note/ })).toBeInTheDocument()
  })

  it('asks for a note before a manager can reject', async () => {
    server.context = MANAGER
    server.tasks = [task({ status: 'proposed', result_version_id: 'v2' })]
    screenFor(<ReviewTaskPanel versions={versions} />)
    fireEvent.click(await screen.findByRole('button', { name: /Tasks/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Show task details' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reject…' }))
    const send = screen.getByRole('button', { name: 'Reject and send back' })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Why it is rejected'), { target: { value: 'Still too long' } })
    fireEvent.click(send)
    await waitFor(() => expect(server.posts).toEqual([['/ext/tasks/t1/reject', { note: 'Still too long' }]]))
  })
})

describe('beside the version switcher', () => {
  it('marks an internal version and lets only a manager publish it', async () => {
    server.context = EDITOR
    useReviewStore.setState({ currentVersion: versions[1] as never })
    const first = screenFor(<VersionVisibility onPublished={vi.fn()} />)
    expect(await screen.findByText('Internal')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Publish to client' })).toBeNull()
    first.unmount()

    server.context = MANAGER
    const onPublished = vi.fn()
    screenFor(<VersionVisibility onPublished={onPublished} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Publish to client' }))
    const confirm = await screen.findAllByRole('button', { name: 'Publish to client' })
    fireEvent.click(confirm[confirm.length - 1])
    await waitFor(() => expect(server.posts).toEqual([['/ext/versions/v2/publish', undefined]]))
    await waitFor(() => expect(onPublished).toHaveBeenCalled())
  })

  it('shows nothing for a published version, or to a client', async () => {
    server.context = MANAGER
    useReviewStore.setState({ currentVersion: versions[0] as never })
    screenFor(<><VersionVisibility onPublished={vi.fn()} /><span>loaded</span></>)
    await screen.findByText('loaded')
    expect(screen.queryByText('Internal')).toBeNull()
  })
})

describe('the comment composer on the review screen', () => {
  const composer = (onSubmit = vi.fn().mockResolvedValue(undefined)) =>
    [<CommentInput key="c" assetId="a1" projectId="p1" assetType="video" onSubmit={onSubmit} />, onSubmit] as const

  async function submit(text: string) {
    const textarea = screen.getByPlaceholderText('Leave your comment...')
    fireEvent.change(textarea, { target: { value: text } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(textarea).toHaveValue(''))
  }

  it('starts a manager on Internal and lets them switch to Public', async () => {
    server.context = MANAGER
    const [node, onSubmit] = composer()
    screenFor(node)
    await screen.findByRole('button', { name: 'Internal' })
    await submit('for the team')
    expect(onSubmit.mock.calls[0][5]).toBe('internal')
    fireEvent.click(screen.getByRole('button', { name: 'Internal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Public' }))
    await submit('for the client')
    expect(onSubmit.mock.calls[1][5]).toBe('public')
  })

  it('shows an editor that their comments are internal, with nothing to switch', async () => {
    server.context = EDITOR
    const [node, onSubmit] = composer()
    screenFor(node)
    expect(await screen.findByText('Internal')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Internal' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Public' })).toBeNull()
    await submit('note')
    expect(onSubmit.mock.calls[0][5]).toBe('internal')
  })

  it('shows a client no visibility control and sends public', async () => {
    server.context = CLIENT
    const [node, onSubmit] = composer()
    screenFor(node)
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Public' })).toBeNull())
    expect(screen.queryByText('Internal')).toBeNull()
    await submit('feedback')
    expect(onSubmit.mock.calls[0][5]).toBe('public')
  })
})
