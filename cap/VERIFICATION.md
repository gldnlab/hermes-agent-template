# Verified native Cap workflow

## Slack feedback rollout — permission still required

Implementation `98b840b`, Cap deployment
`5abdf442-dd72-4762-bfc9-bc502d93f2b2` (successful).

- 81 local tests pass. Installed-Hermes smoke checks passed for per-message run
  binding, queued follow-ups, full event-specific run summaries and native patch
  compatibility. Normal DMs and other services retain native reaction hooks.
- Only a thread's first accepted request gets the board/task introduction;
  follow-ups have an empty acknowledgement body. Errors remain visible.
- Native notification replay for vw-dashboards task `t_b63a971b`, review event
  12/run 2, delivered the full 1,547-character answer in Slack message
  `1789018133.481629`. Slack API read-back confirmed both the caveat and final
  verification note. The task stayed in Review; no coding rerun was started.
- Live reaction verification is BLOCKED: Slack returned `missing_scope`, needed
  `reactions:write`. Cap app `A0C0FNJELBY` currently has `reactions:read` but lacks
  write permission. Reactions are enabled in config. The deployed watcher logs
  the permission failure and retains retries with backoff; coding/result delivery
  continue independently. Do not report live reactions as verified until the
  operator adds the bot scope and reauthorizes/reinstalls Cap.
- A clearly labeled no-coding reaction-test message was posted in Derek's Cap
  DM; its identity is saved in `/data/cap/feedback-verification-98b840b.json`.
  The first reaction API attempt failed before any status changes succeeded.
- Team/Owners deployment IDs are unchanged. No production env vars, tokens or
  Slack app permissions were changed during this rollout.

## Enforced routing rollout

Implementation: `e5abe92`, deployed only to Hermes-Cap as
`d93ea9e8-e733-4f74-a95c-3b84c473606f`.

- 66 local tests pass. The installed-Hermes smoke test also passes with
  isolated boards and real local Git worktrees: deduplication, sticky preparation
  hold, separate boards, native subscriptions, Review follow-ups, scoped PR
  guard exemption, and preservation of authentication guards.
- Existing vw-site thread `1788976794.031949` is registered as native task
  `t_631463fd`, with its six historical Slack text messages imported as comments.
- PR #15 and branch `design/deep-mocha-textures` were preserved. Its checkout
  was copied from `/tmp/vw-mocha` to the persistent volume and Git's worktree
  metadata repaired. The old temporary checkout's `.git` pointer was renamed
  `.git.before-persistent-move`; no application source was deleted.
- A clearly labeled read-only setup request was posted by Cap's own bot in the
  existing thread (`1789015006.262799`) and ingested through the router. This
  exercised native worker dispatch and result delivery, not a new human Slack
  ingress event. Authorization/ingress selection is covered by local tests.
- The durable request survived the follow-up deployment, dispatched native
  Codex run 4, and returned the task to Review. The worker confirmed actual
  `pwd`, branch, HEAD, clean status and GitHub PR state through commands.
- Slack read-back verified the router acknowledgement and native Review result
  `CAP_ROUTING_VERIFIED` at `1789015410.386849`. The complete result is retained
  on the native card; the native Slack notifier sends the first summary line.
- Independent Git and GitHub reads after the worker finished confirmed clean
  checkout, unchanged SHA `2262ad1041e782ff608b89bea392b78767cea9df`, and PR #15
  still open, draft and unmerged. No application edits, commits, pushes or merges
  were performed by the verification. The existing preview was not revalidated.
- Team stayed on `f3a86410-0e05-4e00-90d9-aa4d237e453a`; Owners stayed on
  `8764a80b-1b82-499c-8dc0-4983271624fc`. No production env vars were changed.

Known boundaries: routed entry and worker launch are deterministic, but Cap's
approved full-container command access is unchanged. This is not filesystem
confinement or a GitHub-enforced merge approval gate. Acknowledgements are
at-least-once if Slack accepts a send immediately before a DB/process failure.

## Earlier infrastructure-only verification

Source implementation: `171df57` (branch `cap/railway-native`).
Runtime deployment: `44ff30a3-73b8-47cd-a0ea-b8b8d91662e6`, Hermes-Cap only.

Native Codex MCP created task `t_6c2d0b5a` on board `vw-site`.
The gateway automatically dispatched two runs (initial + explicit setup
follow-up), both using the same native linked worktree and task branch:

- Worktree: `/data/cap/repos/vw-site/.worktrees/t_6c2d0b5a`
- Branch: `wt/t_6c2d0b5a`
- Both workers performed actual file-edit/command assertions and authenticated
  GitHub API calls, and moved their own card into Review.
- Automatic review dispatch stayed off; the card remained in Review.
- The second run's native Review notification was read back from the dedicated
  setup-test thread in Derek's verified Cap DM.
- No application source changed; no code was committed, pushed, or merged in
  any application repository during the verification.
- Team/Owners retained their original deployments throughout.

Four native boards and checkouts are configured: vw-site, vw-hq, vw-crm,
vw-dashboards. New tasks must use their corresponding board and worktree mode.

## Preview and approval limits

Existing vw-site PR #13 has a successful Vercel Preview deployment:
`https://vw-site-81nzynicr-gldnlab.vercel.app`.
The URL redirects unauthenticated visitors to Vercel login. GitHub deployment
status and the URL are verified, but the rendered page was not visually
verified by this setup test. No Vercel protection or secrets were changed.

The supplied GH_TOKEN reports admin-level repo access. Human approval is
required by Cap's instructions and automatic review dispatch is disabled;
this is not a GitHub-enforced merge-authorization boundary.

This infrastructure test did not create a new application PR or test a merge.
