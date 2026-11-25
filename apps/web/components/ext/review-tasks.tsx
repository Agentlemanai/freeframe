'use client'

/**
 * Garlomedia fork: the review-task pieces of the review screen.
 *
 *   <CommentExtActions>      on a comment: "Make task", and for a manager
 *                            "Show to client" / "Make internal"
 *   <MakeTaskDialog>         the form "Make task" opens
 *   <ReviewTaskPanel>        the video's tasks, above its comments
 *   <VersionVisibility>      beside the version switcher: the "Internal" badge
 *                            and "Publish to client"
 *
 * Each renders nothing for a client, and nothing outside <ReviewExtProvider>.
 */

import * as React from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import useSWR, { mutate as globalMutate } from 'swr'
import { Check, ChevronDown, ChevronRight, Eye, EyeOff, ListPlus, Lock, Send, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useToast } from '@/components/shared/toast'
import { useReviewStore } from '@/stores/review-store'
import type { AssetVersion } from '@/types'
import {
  AGENT_STAGES,
  TASK_KINDS,
  TASK_STAGES,
  TASK_STATUS_LABEL,
  assigneeLabel,
  commentsKey,
  kindLabel,
  stageLabel,
  useReviewExt,
  type ReviewTask,
  type ReviewTaskNote,
  type TaskStatus,
} from './review-ext'
import { FixCheckSummary, revalidateFixChecks, useFixCheckGate } from './fix-check'

function errorText(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown; message?: unknown } | null)?.detail
  if (typeof detail === 'string' && detail) return detail
  const message = (error as { message?: unknown } | null)?.message
  return typeof message === 'string' && message ? message : fallback
}

const STATUS_TONE: Record<TaskStatus, string> = {
  open: 'bg-bg-tertiary text-text-secondary',
  claimed: 'bg-status-info/15 text-status-info',
  in_progress: 'bg-status-info/15 text-status-info',
  proposed: 'bg-amber-400/15 text-amber-400',
  needs_info: 'bg-status-warning/15 text-status-warning',
  approved: 'bg-status-success/15 text-status-success',
  cancelled: 'bg-bg-tertiary text-text-tertiary line-through',
}

export function TaskStatusPill({ status, className }: { status: TaskStatus; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium leading-none whitespace-nowrap',
        STATUS_TONE[status],
        className,
      )}
    >
      {TASK_STATUS_LABEL[status]}
    </span>
  )
}

// ─── On a comment ─────────────────────────────────────────────────────────────

interface CommentLike {
  id: string
  body: string
  visibility?: string
  version_id?: string
}

/**
 * Mounted in every comment row, including on share pages and in the compare
 * view. The guard comes before any other hook on purpose: outside the review
 * screen there is no context, and no toast provider either.
 */
export function CommentExtActions({ comment, depth = 0 }: { comment: CommentLike; depth?: number }) {
  const ext = useReviewExt()
  if (!ext || !ext.info.sees_internal) return null
  return <TeamCommentActions comment={comment} depth={depth} />
}

function TeamCommentActions({ comment, depth }: { comment: CommentLike; depth: number }) {
  const ext = useReviewExt()
  const toast = useToast()
  const [busy, setBusy] = React.useState(false)
  if (!ext) return null

  const task = ext.tasks.find((t) => t.source_comment_id === comment.id)
  const isInternal = comment.visibility === 'internal'

  // Every comment is a task by itself; one that asks for nothing (praise, chat) is dismissed here.
  async function notATask() {
    if (!ext || !task) return
    setBusy(true)
    try {
      await api.post(`/ext/tasks/${task.id}/dismiss`, {})
      await ext.refreshTasks()
      toast.success('Not a task: dismissed. The comment stays.')
    } catch (error) {
      toast.error(errorText(error, 'Could not dismiss this task'))
    } finally {
      setBusy(false)
    }
  }

  async function setVisibility(next: 'public' | 'internal') {
    if (!ext) return
    setBusy(true)
    try {
      await api.patch(`/ext/comments/${comment.id}/visibility`, { visibility: next })
      if (comment.version_id) await globalMutate(commentsKey(ext.assetId, comment.version_id))
      toast.success(next === 'public' ? 'The client can now see this comment' : 'Only the team sees this comment now')
    } catch (error) {
      toast.error(errorText(error, 'Could not change who sees this comment'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {task ? (
        <>
          <TaskStatusPill status={task.status} />
          {task.status === 'open' && (
            <button
              type="button"
              disabled={busy}
              title="Not a task"
              aria-label="Not a task"
              onClick={notATask}
              className="inline-flex h-5 w-5 items-center justify-center rounded text-text-tertiary hover:bg-bg-hover hover:text-text-primary transition-colors disabled:opacity-50"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </>
      ) : (
        depth === 0 && (
          <button
            type="button"
            title="Make task"
            aria-label="Make task"
            onClick={() => ext.openMakeTask({ id: comment.id, body: comment.body })}
            className="inline-flex h-5 w-5 items-center justify-center rounded text-text-tertiary hover:bg-bg-hover hover:text-text-primary transition-colors"
          >
            <ListPlus className="h-3.5 w-3.5" />
          </button>
        )
      )}
      {ext.info.is_manager && (
        <button
          type="button"
          disabled={busy}
          title={isInternal ? 'Show to client' : 'Make internal'}
          aria-label={isInternal ? 'Show to client' : 'Make internal'}
          onClick={() => setVisibility(isInternal ? 'public' : 'internal')}
          className="inline-flex h-5 w-5 items-center justify-center rounded text-text-tertiary hover:bg-bg-hover hover:text-text-primary transition-colors disabled:opacity-50"
        >
          {isInternal ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
        </button>
      )}
    </>
  )
}

// ─── Make task ────────────────────────────────────────────────────────────────

const fieldClass =
  'w-full rounded-md border border-border bg-bg-primary px-2.5 py-1.5 text-[13px] text-text-primary outline-none focus:border-border-focus'
const labelClass = 'block text-xs font-medium text-text-secondary mb-1'

export function MakeTaskDialog() {
  const ext = useReviewExt()
  if (!ext || !ext.info.sees_internal) return null
  const source = ext.makeTaskFor
  return (
    <Dialog.Root open={source !== null} onOpenChange={(open) => !open && ext.openMakeTask(null)}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border bg-bg-secondary p-5 shadow-xl">
          <Dialog.Title className="text-sm font-semibold text-text-primary">Make task</Dialog.Title>
          <Dialog.Description className="mt-1 text-xs text-text-tertiary">
            The task and its notes stay with the team. The client never sees them.
          </Dialog.Description>
          {/* Keyed by the comment, so each one opens a fresh form with its own text. */}
          {source && <MakeTaskForm key={source.id} source={source} />}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function MakeTaskForm({ source }: { source: { id: string; body: string } }) {
  const ext = useReviewExt()
  const toast = useToast()
  const [instruction, setInstruction] = React.useState(source.body)
  const [kind, setKind] = React.useState<string>('cut_trim')
  const [stage, setStage] = React.useState<string>('review')
  const [picked, setPicked] = React.useState<string>('pool')
  const [saving, setSaving] = React.useState(false)
  const [problem, setProblem] = React.useState<string | null>(null)
  if (!ext) return null

  // An agent cannot be picked at a stage that is for people only; a choice of
  // agent made at another stage falls back to the editors' pool.
  const agentAllowed = AGENT_STAGES.includes(stage)
  const assignee = !agentAllowed && picked === 'agent' ? 'pool' : picked

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!ext) return
    setSaving(true)
    setProblem(null)
    try {
      await api.post('/ext/tasks', {
        asset_id: ext.assetId,
        source_comment_id: source.id,
        instruction: instruction.trim(),
        kind,
        stage,
        ...(assignee === 'agent'
          ? { assignee_type: 'agent' }
          : assignee === 'pool'
            ? { assignee_type: 'role_pool', assignee_role: 'editor' }
            : { assignee_type: 'person', assignee_person_id: assignee }),
      })
      await ext.refreshTasks()
      ext.openMakeTask(null)
      toast.success('Task created')
    } catch (error) {
      setProblem(errorText(error, 'Could not create the task'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-4 space-y-3">
      <div>
        <label className={labelClass} htmlFor="ext-task-instruction">What needs changing</label>
        <textarea
          id="ext-task-instruction"
          className={cn(fieldClass, 'min-h-[72px] resize-y')}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          required
          autoFocus
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelClass} htmlFor="ext-task-kind">Kind</label>
          <select id="ext-task-kind" className={fieldClass} value={kind} onChange={(e) => setKind(e.target.value)}>
            {TASK_KINDS.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className={labelClass} htmlFor="ext-task-stage">Stage</label>
          <select id="ext-task-stage" className={fieldClass} value={stage} onChange={(e) => setStage(e.target.value)}>
            {TASK_STAGES.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
      </div>
      <div>
        <label className={labelClass} htmlFor="ext-task-assignee">Who does it</label>
        <select
          id="ext-task-assignee"
          className={fieldClass}
          value={assignee}
          onChange={(e) => setPicked(e.target.value)}
        >
          <option value="pool">Any editor on this project</option>
          {ext.team.map((member) => (
            <option key={member.id} value={member.id}>{member.name}</option>
          ))}
          <option value="agent" disabled={!agentAllowed}>
            {agentAllowed ? 'AI agent' : `AI agent (not at ${stageLabel(stage)}: people only)`}
          </option>
        </select>
      </div>
      {problem && <p role="alert" className="text-xs text-status-error">{problem}</p>}
      <div className="flex justify-end gap-2 pt-1">
        <Button type="button" variant="secondary" size="sm" onClick={() => ext.openMakeTask(null)}>
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={saving || !instruction.trim()}>
          {saving ? 'Creating…' : 'Create task'}
        </Button>
      </div>
    </form>
  )
}

// ─── The video's tasks ────────────────────────────────────────────────────────

const FINISHED: TaskStatus[] = ['approved', 'cancelled']

function TaskRow({ task, versions }: { task: ReviewTask; versions: AssetVersion[] }) {
  const ext = useReviewExt()
  const toast = useToast()
  const setCurrentVersion = useReviewStore((s) => s.setCurrentVersion)
  const [open, setOpen] = React.useState(false)
  const [rejecting, setRejecting] = React.useState(false)
  const [text, setText] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  // Fetched when the row is opened, and again when the task changes underneath it.
  const { data: notes, mutate: loadNotes } = useSWR<ReviewTaskNote[]>(
    open ? `/ext/tasks/${task.id}/notes?at=${task.status}` : null,
    () => api.get<ReviewTaskNote[]>(`/ext/tasks/${task.id}/notes`),
    { revalidateOnFocus: false },
  )

  if (!ext) return null
  const result = versions.find((v) => v.id === task.result_version_id)
  const canDecide = ext.info.is_manager && task.status === 'proposed'

  async function act(run: () => Promise<unknown>, done: string, failed: string) {
    if (!ext) return
    setBusy(true)
    try {
      await run()
      await ext.refreshTasks()
      await loadNotes()
      setText('')
      setRejecting(false)
      toast.success(done)
    } catch (error) {
      toast.error(errorText(error, failed))
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="border-t border-border first:border-t-0">
      <div className="flex items-start gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={open ? 'Hide task details' : 'Show task details'}
          className="mt-0.5 text-text-tertiary hover:text-text-primary"
        >
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
        <div className="min-w-0 flex-1">
          <p className={cn('text-[13px] text-text-primary', !open && 'line-clamp-2')}>{task.instruction}</p>
          <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px] text-text-tertiary">
            <TaskStatusPill status={task.status} />
            <span>{kindLabel(task.kind)}</span>
            <span aria-hidden>·</span>
            <span>{assigneeLabel(task, ext.team)}</span>
            {task.claimed_by && (
              <>
                <span aria-hidden>·</span>
                <span>{task.claimed_by}</span>
              </>
            )}
          </p>
        </div>
        {/* One next step per row; everything else is inside. */}
        {canDecide && !rejecting && (
          <Button
            size="sm"
            disabled={busy}
            onClick={() => act(() => api.post(`/ext/tasks/${task.id}/approve`, {}), 'Task approved', 'Could not approve')}
          >
            <Check className="h-3.5 w-3.5" />
            Approve
          </Button>
        )}
      </div>

      {open && (
        <div className="space-y-2 px-3 pb-3 pl-8">
          <p className="text-[11px] text-text-tertiary">
            {stageLabel(task.stage)}
            {task.frame_in !== null && (
              <> · frame {task.frame_in}{task.frame_out !== null && task.frame_out !== task.frame_in ? `–${task.frame_out}` : ''}</>
            )}
          </p>
          {result && (
            <button
              type="button"
              className="text-xs text-accent hover:underline"
              onClick={() => setCurrentVersion(result)}
            >
              Open the proposed version (v{result.version_number})
            </button>
          )}
          {notes && notes.length > 0 && (
            <ul className="space-y-1.5">
              {notes.map((note) => (
                <li key={note.id} className="rounded-md bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-secondary">
                  <span className="font-medium text-text-primary">
                    {note.author_type === 'agent'
                      ? (note.author_label ?? 'AI agent')
                      : (ext.team.find((m) => m.id === note.author_person_id)?.name ?? note.author_label ?? 'Team')}
                  </span>{' '}
                  <span className="whitespace-pre-wrap">{note.body}</span>
                </li>
              ))}
            </ul>
          )}
          {!FINISHED.includes(task.status) && (
            <div className="space-y-1.5">
              <textarea
                className={cn(fieldClass, 'min-h-[52px] resize-y text-xs')}
                placeholder={rejecting ? 'What is still wrong? The editor gets this note.' : 'Note for the team'}
                aria-label={rejecting ? 'Why it is rejected' : 'Note for the team'}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <div className="flex flex-wrap justify-end gap-1.5">
                {canDecide && !rejecting && (
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => setRejecting(true)}>
                    Reject…
                  </Button>
                )}
                {rejecting ? (
                  <>
                    <Button size="sm" variant="secondary" disabled={busy} onClick={() => setRejecting(false)}>
                      Back
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={busy || !text.trim()}
                      onClick={() =>
                        act(
                          () => api.post(`/ext/tasks/${task.id}/reject`, { note: text.trim() }),
                          'Sent back to the editor',
                          'Could not reject',
                        )
                      }
                    >
                      Reject and send back
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={busy || !text.trim()}
                    onClick={() =>
                      act(
                        () => api.post(`/ext/tasks/${task.id}/notes`, { kind: 'reply', body: text.trim() }),
                        'Note added',
                        'Could not add the note',
                      )
                    }
                  >
                    <Send className="h-3 w-3" />
                    Add note
                  </Button>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  )
}

export function ReviewTaskPanel({ versions }: { versions: AssetVersion[] }) {
  const ext = useReviewExt()
  const [open, setOpen] = React.useState(false)
  if (!ext || !ext.info.sees_internal || ext.tasks.length === 0) return null

  const live = ext.tasks.filter((t) => !FINISHED.includes(t.status))
  const waiting = ext.tasks.filter((t) => t.status === 'proposed').length
  const questions = ext.tasks.filter((t) => t.status === 'needs_info').length
  // "active", not "open": open is also the name of one status.
  const summary = [
    `${live.length} active`,
    waiting > 0 && (ext.info.is_manager ? `${waiting} for you to approve` : `${waiting} waiting for approval`),
    questions > 0 && `${questions} question${questions === 1 ? '' : 's'}`,
  ].filter(Boolean).join(' · ')
  // What needs someone first, finished work last.
  const order: TaskStatus[] = ['proposed', 'needs_info', 'in_progress', 'claimed', 'open', 'approved', 'cancelled']
  const sorted = [...ext.tasks].sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status))

  return (
    <section aria-label="Tasks" className="border-b border-border bg-bg-secondary">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5 text-text-tertiary" /> : <ChevronRight className="h-3.5 w-3.5 text-text-tertiary" />}
        <span className="text-[13px] font-medium text-text-primary">Tasks</span>
        <span className={cn('text-xs', waiting > 0 && ext.info.is_manager ? 'text-amber-400' : 'text-text-tertiary')}>
          {summary}
        </span>
        <Lock className="ml-auto h-3 w-3 text-amber-400" aria-label="Team only" />
      </button>
      {open && (
        <ul className="max-h-[40vh] overflow-y-auto">
          {sorted.map((task) => (
            <TaskRow key={task.id} task={task} versions={versions} />
          ))}
        </ul>
      )}
    </section>
  )
}

// ─── Beside the version switcher ──────────────────────────────────────────────

export function VersionVisibility({ onPublished }: { onPublished: () => unknown }) {
  const ext = useReviewExt()
  const version = useReviewStore((s) => s.currentVersion)
  if (!ext || !ext.info.sees_internal || !version || version.visibility !== 'internal') return null
  return <InternalVersion version={version} isManager={ext.info.is_manager} onPublished={onPublished} />
}

function InternalVersion({
  version,
  isManager,
  onPublished,
}: {
  version: AssetVersion
  isManager: boolean
  onPublished: () => unknown
}) {
  const toast = useToast()
  const [confirming, setConfirming] = React.useState(false)
  const [busy, setBusy] = React.useState(false)
  const gate = useFixCheckGate(version)

  const ready = version.processing_status === 'ready'

  async function publish() {
    setBusy(true)
    try {
      await api.post(`/ext/versions/${version.id}/publish${gate.unchecked > 0 ? '?force=true' : ''}`)
      await onPublished()
      revalidateFixChecks()
      setConfirming(false)
      toast.success(`v${version.version_number} is now visible to the client`)
    } catch (error) {
      toast.error(errorText(error, 'Could not publish this version'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <span
        className="inline-flex items-center gap-1 rounded-md border border-amber-400/30 bg-amber-400/10 px-2 py-1 text-xs text-amber-400"
        title="Only the team sees this version"
      >
        <Lock className="h-3 w-3" />
        Internal
      </span>
      {ready && <FixCheckSummary version={version} />}
      {isManager && ready && (
        <Button size="sm" onClick={() => setConfirming(true)}>
          Publish to client
        </Button>
      )}
      <Dialog.Root open={confirming} onOpenChange={setConfirming}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border bg-bg-secondary p-5 shadow-xl">
            <Dialog.Title className="text-sm font-semibold text-text-primary">
              Publish v{version.version_number} to the client?
            </Dialog.Title>
            <Dialog.Description className="mt-1 text-xs text-text-tertiary">
              The client and anyone with a share link will see this version. Internal comments,
              tasks and notes stay with the team. Comments checked as fixed are resolved.
            </Dialog.Description>
            {gate.unchecked > 0 && (
              <p className="mt-3 rounded-md border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs text-amber-400">
                {gate.unchecked} of {gate.total} earlier comment{gate.total === 1 ? '' : 's'} not checked on this
                version yet. They stay open for the client.
              </p>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <Button type="button" variant="secondary" size="sm" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              {gate.unchecked > 0 && gate.openCheck && (
                <Button type="button" variant="secondary" size="sm" onClick={() => { setConfirming(false); gate.openCheck?.() }}>
                  Check fixes
                </Button>
              )}
              <Button type="button" size="sm" disabled={busy} onClick={publish}>
                {busy ? 'Publishing…' : gate.unchecked > 0 ? 'Publish anyway' : 'Publish to client'}
              </Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  )
}
