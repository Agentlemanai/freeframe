'use client'

/**
 * Garlomedia fork: fix check. Before a new version goes to the client, the team
 * goes through the comments made on earlier versions and marks each fixed, not
 * fixed or won't fix, comparing the two versions side by side. Internal only.
 *
 *   <FixCheckSummary>   beside the version switcher: "Fixes 3/7", opens the check
 *   <FixCheckPanel>     in the compare view's left column while ?check=1
 *
 * The server owns the list (GET /ext/versions/{id}/fix-checks) and refuses to
 * publish while it is incomplete, unless the owner publishes anyway.
 */

import * as React from 'react'
import useSWR, { mutate as globalMutate } from 'swr'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { Check, ListChecks, Lock, MessageSquare, Minus, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatTime } from '@/lib/utils'
import { useToast } from '@/components/shared/toast'
import type { AssetVersion } from '@/types'
import { TASK_STATUS_LABEL, useReviewExt, type TaskStatus } from './review-ext'

export type FixStatus = 'fixed' | 'not_fixed' | 'wont_fix'

export interface FixCheckItem {
  comment_id: string
  version_id: string
  version_number: number | null
  timecode_start: number | null
  timecode_end: number | null
  body: string
  author_name: string | null
  visibility: 'public' | 'internal'
  drawing: Record<string, unknown> | null
  task_id: string | null
  task_status: TaskStatus | null
  not_fixed_in: number | null
  status: FixStatus | null
  note: string | null
  checked_by_name: string | null
  checked_at: string | null
}

export interface FixChecklist {
  version_id: string
  version_number: number
  previous_version_id: string | null
  total: number
  checked: number
  items: FixCheckItem[]
}

export const fixChecksKey = (versionId: string) => `/ext/versions/${versionId}/fix-checks`

/** Every open checklist, after the server says the team changed something. */
export function revalidateFixChecks(): void {
  void globalMutate((key) => typeof key === 'string' && key.startsWith('/ext/versions/') && key.endsWith('/fix-checks'))
}

/** The checklist of an internal version, for the team; null for anyone else. */
export function useFixChecklist(version: Pick<AssetVersion, 'id' | 'visibility'> | null | undefined) {
  const ext = useReviewExt()
  const enabled = Boolean(ext?.info.sees_internal && version)
  return useSWR<FixChecklist>(
    enabled ? fixChecksKey((version as AssetVersion).id) : null,
    (key: string) => api.get<FixChecklist>(key),
    { revalidateOnFocus: false },
  )
}

/** Whether the compare view should show the fix check instead of the left comments. */
export function useFixCheckMode(): boolean {
  const ext = useReviewExt()
  const searchParams = useSearchParams()
  return searchParams.get('check') === '1' && Boolean(ext?.info.sees_internal)
}

function useOpenFixCheck() {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  return React.useCallback(
    (left: string, right: string) => {
      const p = new URLSearchParams(searchParams.toString())
      p.set('compare', left)
      p.set('right', right)
      p.set('check', '1')
      router.replace(`${pathname}?${p.toString()}`, { scroll: false })
    },
    [router, pathname, searchParams],
  )
}

// ─── Beside the version switcher ──────────────────────────────────────────────

export function FixCheckSummary({ version }: { version: AssetVersion }) {
  const { data } = useFixChecklist(version)
  const open = useOpenFixCheck()
  if (!data || data.total === 0 || !data.previous_version_id) return null
  const done = data.checked === data.total
  return (
    <button
      type="button"
      onClick={() => open(data.previous_version_id as string, version.id)}
      className={cn(
        'inline-flex h-8 items-center gap-1.5 whitespace-nowrap rounded-md border px-2.5 text-xs font-medium transition-colors',
        done
          ? 'border-status-success/30 bg-status-success/10 text-status-success'
          : 'border-amber-400/30 bg-amber-400/10 text-amber-400 hover:bg-amber-400/20',
      )}
      title="Check the earlier comments on this version before it goes to the client"
    >
      <ListChecks className="h-3.5 w-3.5" />
      Fixes {data.checked}/{data.total}
    </button>
  )
}

/** For the publish dialog: what is still unchecked, and a way to go and check it. */
export function useFixCheckGate(version: AssetVersion) {
  const { data } = useFixChecklist(version)
  const open = useOpenFixCheck()
  const unchecked = data ? data.total - data.checked : 0
  return {
    unchecked,
    total: data?.total ?? 0,
    openCheck: data?.previous_version_id ? () => open(data.previous_version_id as string, version.id) : null,
  }
}

// ─── In the compare view ─────────────────────────────────────────────────────

const STATUS_BUTTONS: Array<{ value: FixStatus; label: string; icon: React.ComponentType<{ className?: string }>; on: string }> = [
  { value: 'fixed', label: 'Fixed', icon: Check, on: 'border-status-success/40 bg-status-success/15 text-status-success' },
  { value: 'not_fixed', label: 'Not fixed', icon: X, on: 'border-status-error/40 bg-status-error/15 text-status-error' },
  { value: 'wont_fix', label: "Won't fix", icon: Minus, on: 'border-border-focus bg-bg-hover text-text-primary' },
]

export function FixCheckPanel({
  rightVersionId,
  leftVersionId,
  offset,
  seekTo,
  pause,
  showAnnotation,
}: {
  rightVersionId: string
  leftVersionId: string
  /** The left pane's offset on the shared timeline. */
  offset: number
  seekTo(time: number): void
  pause(): void
  showAnnotation(drawing: Record<string, unknown> | null): void
}) {
  const { data, mutate } = useSWR<FixChecklist>(
    fixChecksKey(rightVersionId),
    (key: string) => api.get<FixChecklist>(key),
    { revalidateOnFocus: false },
  )
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const [active, setActive] = React.useState<string | null>(null)

  function leave() {
    const p = new URLSearchParams(searchParams.toString())
    p.delete('check')
    router.replace(`${pathname}?${p.toString()}`, { scroll: false })
  }

  function focus(item: FixCheckItem) {
    setActive(item.comment_id)
    if (item.timecode_start != null) {
      seekTo(item.timecode_start + offset)
      pause()
    }
    showAnnotation(item.version_id === leftVersionId ? item.drawing : null)
  }

  if (!data) {
    return <div className="p-4 text-xs text-text-tertiary">Loading the fix check…</div>
  }
  const done = data.checked === data.total
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-text-primary">
            <ListChecks className="h-3.5 w-3.5" />
            Fix check · v{data.version_number}
          </span>
          <button
            type="button"
            onClick={leave}
            className="inline-flex items-center gap-1 text-[11px] text-text-tertiary hover:text-text-primary"
            title="Show the left version's comments instead"
          >
            <MessageSquare className="h-3 w-3" />
            Comments
          </button>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-bg-tertiary" aria-hidden>
          <div
            className={cn('h-full transition-all', done ? 'bg-status-success' : 'bg-amber-400')}
            style={{ width: `${data.total ? (100 * data.checked) / data.total : 100}%` }}
          />
        </div>
        <p className="mt-1.5 text-[11px] text-text-tertiary">
          {data.total === 0
            ? 'No earlier comments to check.'
            : done
              ? `All ${data.total} checked. Ready to publish to the client.`
              : `${data.checked} of ${data.total} checked. Only the team sees this.`}
        </p>
      </div>
      <ol className="min-h-0 flex-1 divide-y divide-border overflow-y-auto">
        {data.items.map((item) => (
          <FixCheckRow
            // a saved note from the server starts the row afresh
            key={`${item.comment_id}:${item.note ?? ''}`}
            item={item}
            versionId={rightVersionId}
            active={active === item.comment_id}
            onFocus={() => focus(item)}
            onSaved={() => mutate()}
          />
        ))}
      </ol>
    </div>
  )
}

function FixCheckRow({
  item,
  versionId,
  active,
  onFocus,
  onSaved,
}: {
  item: FixCheckItem
  versionId: string
  active: boolean
  onFocus(): void
  onSaved(): unknown
}) {
  const toast = useToast()
  const [note, setNote] = React.useState(item.note ?? '')
  const [busy, setBusy] = React.useState(false)

  async function save(status: FixStatus | null, nextNote = note) {
    setBusy(true)
    try {
      await api.put(`/ext/versions/${versionId}/fix-checks/${item.comment_id}`, { status, note: nextNote || null })
      await onSaved()
    } catch (error) {
      const detail = (error as { detail?: unknown } | null)?.detail
      toast.error(typeof detail === 'string' && detail ? detail : 'Could not save the check')
    } finally {
      setBusy(false)
    }
  }

  const askNote = item.status === 'not_fixed' || item.status === 'wont_fix' || Boolean(item.note)
  return (
    <li
      className={cn('px-3 py-2.5 transition-colors', active ? 'bg-bg-hover' : 'hover:bg-bg-hover/50')}
      data-testid="fix-check-row"
    >
      <button type="button" onClick={onFocus} className="block w-full text-left">
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-text-tertiary">
          <span className="rounded bg-bg-tertiary px-1.5 py-0.5 font-mono text-text-secondary">
            v{item.version_number}
            {item.timecode_start != null ? ` · ${formatTime(item.timecode_start)}` : ''}
          </span>
          {item.visibility === 'internal' ? (
            <span className="inline-flex items-center gap-0.5 text-amber-400">
              <Lock className="h-2.5 w-2.5" /> Internal
            </span>
          ) : (
            <span className="text-status-info">Client</span>
          )}
          <span className="truncate">{item.author_name ?? 'Someone'}</span>
          {item.not_fixed_in != null && (
            <span className="text-status-error">not fixed in v{item.not_fixed_in}</span>
          )}
          {item.task_status && (
            <span className="text-text-secondary">· task: {TASK_STATUS_LABEL[item.task_status] ?? item.task_status}</span>
          )}
        </div>
        <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-xs text-text-primary">{item.body}</p>
      </button>
      <div className="mt-2 flex flex-wrap gap-1" role="group" aria-label="Fix status">
        {STATUS_BUTTONS.map(({ value, label, icon: Icon, on }) => {
          const selected = item.status === value
          return (
            <button
              key={value}
              type="button"
              disabled={busy}
              aria-pressed={selected}
              onClick={() => save(selected ? null : value)}
              className={cn(
                'inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[11px] font-medium transition-colors disabled:opacity-50',
                selected ? on : 'border-border text-text-secondary hover:bg-bg-hover hover:text-text-primary',
              )}
            >
              <Icon className="h-3 w-3" />
              {label}
            </button>
          )
        })}
      </div>
      {askNote && (
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onBlur={() => {
            if ((item.note ?? '') !== note && item.status) void save(item.status, note)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          }}
          placeholder={item.status === 'wont_fix' ? 'Why not (for the team)' : 'What is still wrong'}
          aria-label="Note"
          className="mt-1.5 h-7 w-full rounded-md border border-border bg-bg-tertiary px-2 text-[11px] text-text-primary placeholder:text-text-tertiary focus:border-border-focus focus:outline-none"
        />
      )}
      {item.checked_by_name && (
        <p className="mt-1 text-[10px] text-text-tertiary">
          {item.checked_by_name}
          {item.checked_at ? ` · ${new Date(item.checked_at).toLocaleString()}` : ''}
        </p>
      )}
    </li>
  )
}
