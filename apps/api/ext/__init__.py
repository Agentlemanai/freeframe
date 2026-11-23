"""Garlomedia extensions to FreeFrame: review tasks, client / internal
visibility, the event outbox and the gateway service account.

Everything the fork adds for review-driven editing lives in this package, with
its own tables and migrations, so merging upstream stays cheap. The few edits
to upstream files are one-line calls into this package; each is listed in
`docs/ext-review-tasks.md`.
"""
