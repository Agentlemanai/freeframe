import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SWRConfig } from 'swr'
import { useReviewStore } from '@/stores/review-store'

// What the server answers, per test (apps/api/ext/fixcheck.py decides all of it).
const server = vi.hoisted(() => ({
  context: null as Record<string, unknown> | null,
  checklist: null as Record<string, unknown> | null,
  posts: [] as Array<[string, unknown]>,
  puts: [] as Array<[string, unknown]>,
}))
const nav = vi.hoisted(() => ({ query: '', replace: vi.fn() }))

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(async (url: string) => {
      if (url.endsWith('/context')) {
        if (!server.context) throw Object.assign(new Error('Not Found'), { status: 404 })
        return server.context
      }
      if (url.endsWith('/fix-checks')) return server.checklist
      return []
    }),
    post: vi.fn(async (url: string, body: unknown) => {
      server.posts.push([url, body])
      return {}
    }),
    put: vi.fn(async (url: string, body: unknown) => {
      server.puts.push([url, body])
      const item = (server.checklist?.items as Array<Record<string, unknown>>).find((i) => url.endsWith(String(i.comment_id)))
      if (item) Object.assign(item, body)
      return item
    }),
  },
}))
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  usePathname: () => '/projects/p1/assets/a1',
  useSearchParams: () => new URLSearchParams(nav.query),
}))
vi.mock('@/components/shared/toast', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
}))
vi.mock('@/components/review/review-provider', () => ({
  useReview: () => ({ pauseVideo: vi.fn(), registerPauseHandler: vi.fn() }),
}))

import { ReviewExtProvider } from '../review-ext'
import { VersionVisibility } from '../review-tasks'
import { FixCheckPanel } from '../fix-check'

const MANAGER = { sees_internal: true, is_manager: true, default_comment_visibility: 'internal', can_choose_comment_visibility: true }
const CLIENT = { sees_internal: false, is_manager: false, default_comment_visibility: 'public', can_choose_comment_visibility: false }
const v2 = { id: 'v2', asset_id: 'a1', version_number: 2, processing_status: 'ready', visibility: 'internal' }

const item = (over: Record<string, unknown>) => ({
  comment_id: 'c1', version_id: 'v1', version_number: 1, timecode_start: 2, timecode_end: null,
  body: 'Too long at the start', author_name: 'Rita Client', visibility: 'public', drawing: null,
  task_id: null, task_status: null, not_fixed_in: null, status: null, note: null,
  checked_by_name: null, checked_at: null, ...over,
})

function screenFor(children: React.ReactNode) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <ReviewExtProvider assetId="a1" projectId="p1">{children}</ReviewExtProvider>
    </SWRConfig>,
  )
}

beforeEach(() => {
  server.context = MANAGER
  server.posts = []
  server.puts = []
  nav.query = ''
  nav.replace.mockClear()
  server.checklist = {
    version_id: 'v2', version_number: 2, previous_version_id: 'v1', total: 2, checked: 0,
    items: [
      item({}),
      item({ comment_id: 'c2', body: 'Logo too small', visibility: 'internal', author_name: 'Olga Owner', timecode_start: 4, drawing: { d: 1 }, not_fixed_in: 1 }),
    ],
  }
  useReviewStore.getState().reset()
})

describe('beside the version switcher', () => {
  it('shows how far the check is and opens it on the compare view', async () => {
    useReviewStore.setState({ currentVersion: v2 as never })
    screenFor(<VersionVisibility onPublished={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: /Fixes 0\/2/ }))
    expect(nav.replace).toHaveBeenCalledWith('/projects/p1/assets/a1?compare=v1&right=v2&check=1', { scroll: false })
  })

  it('warns before publishing with unchecked comments, and publishes anyway only on purpose', async () => {
    useReviewStore.setState({ currentVersion: v2 as never })
    screenFor(<VersionVisibility onPublished={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Publish to client' }))
    expect(await screen.findByText(/2 of 2 earlier comments not checked/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check fixes' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Publish anyway' }))
    await waitFor(() => expect(server.posts).toEqual([['/ext/versions/v2/publish?force=true', undefined]]))
  })

  it('publishes normally once everything is checked', async () => {
    server.checklist = { ...server.checklist!, checked: 2 }
    useReviewStore.setState({ currentVersion: v2 as never })
    screenFor(<VersionVisibility onPublished={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /Fixes 2\/2/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Publish to client' }))
    const confirm = await screen.findAllByRole('button', { name: 'Publish to client' })
    fireEvent.click(confirm[confirm.length - 1])
    await waitFor(() => expect(server.posts).toEqual([['/ext/versions/v2/publish', undefined]]))
  })

  it('shows a client nothing', async () => {
    server.context = CLIENT
    useReviewStore.setState({ currentVersion: v2 as never })
    screenFor(<><VersionVisibility onPublished={vi.fn()} /><span>loaded</span></>)
    await screen.findByText('loaded')
    expect(screen.queryByText(/Fixes/)).toBeNull()
  })
})

describe('in the compare view', () => {
  function panel() {
    const calls = { seek: vi.fn(), pause: vi.fn(), annotation: vi.fn() }
    screenFor(
      <FixCheckPanel rightVersionId="v2" leftVersionId="v1" offset={0.5}
        seekTo={calls.seek} pause={calls.pause} showAnnotation={calls.annotation} />,
    )
    return calls
  }

  it('lists the earlier comments, client and internal, and jumps to each', async () => {
    const calls = panel()
    expect(await screen.findByText('Too long at the start')).toBeInTheDocument()
    expect(screen.getByText('0 of 2 checked. Only the team sees this.')).toBeInTheDocument()
    expect(screen.getByText('not fixed in v1')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Logo too small'))
    expect(calls.seek).toHaveBeenCalledWith(4.5)
    expect(calls.pause).toHaveBeenCalled()
    expect(calls.annotation).toHaveBeenCalledWith({ d: 1 })
  })

  it('marks a comment fixed, and asks why when it is not', async () => {
    panel()
    const rows = await screen.findAllByTestId('fix-check-row')
    fireEvent.click(rows[0].querySelector('button[aria-pressed]') as HTMLButtonElement) // Fixed
    await waitFor(() => expect(server.puts[0]).toEqual(['/ext/versions/v2/fix-checks/c1', { status: 'fixed', note: null }]))
    fireEvent.click(screen.getAllByRole('button', { name: 'Not fixed' })[1])
    await waitFor(() => expect(server.puts[1]).toEqual(['/ext/versions/v2/fix-checks/c2', { status: 'not_fixed', note: null }]))
    const note = await screen.findByLabelText('Note')
    fireEvent.change(note, { target: { value: 'still tiny' } })
    fireEvent.blur(note)
    await waitFor(() => expect(server.puts[2]).toEqual(['/ext/versions/v2/fix-checks/c2', { status: 'not_fixed', note: 'still tiny' }]))
  })

  it('clears a check when the chosen button is pressed again', async () => {
    server.checklist!.items = [item({ status: 'fixed' })]
    panel()
    fireEvent.click(await screen.findByRole('button', { name: 'Fixed', pressed: true }))
    await waitFor(() => expect(server.puts[0]).toEqual(['/ext/versions/v2/fix-checks/c1', { status: null, note: null }]))
  })
})
