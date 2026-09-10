# Hermes-Cap Railway pilot

Separate service, separate `/data` volume, same Hermes template. Atlas/Donna in
Hermes-Team and Hermes-Owners are not moved, reconfigured, or redeployed.

## Deployment

Source branch: `cap/railway-native`. Deploy this branch explicitly to
`Hermes-Cap`; it is not linked to `main` auto-deployment. The Cap startup guard
rejects another Railway service name. Codex CLI is pinned to `0.153.4` in the
image. Hermes retains the template's pinned version and dashboard.

Bootstrap creates missing files only. Cap's default Hermes profile lives in
`/data/.hermes`; native Codex login/session state uses `/data/.codex` (the image
sets HOME=/data). The service must also set `CODEX_HOME=/data/.codex`: Hermes
changes HOME for its subprocesses, so HOME alone points the runtime at the wrong
login directory. Workspaces live under `/data/cap`. Other agents' auth is not
copied automatically; the operator-approved Sheets account is described below.

## Client source Google Sheets

With Derek's approval, Cap uses Vee's existing client-data service account via
`GOOGLE_SERVICE_ACCOUNT_EMAIL` and `GOOGLE_SERVICE_API_KEY` (a PEM private key,
despite the variable name). No personal OAuth tokens are copied. Startup
validates and materializes these two values to the app-owned file
`/data/cap/credentials/google-sheets.json` (0600; parent 0700), outside all repos.
Codex commands need not inherit private-key environment variables. The helper
`python3 /app/cap/sheets.py` supports `metadata` and `read` only, requests the
Sheets read-only scope, and never prints credentials or raw upstream errors.
SOUL and each routed worker prompt point to the helper. See `sheets-guide.md`.
This is not enforced read-only access for arbitrary commands in the container:
the shared service account may have wider Google permissions. Do not represent
helper restrictions as a sandbox. No client source writes are authorized.

## Approved execution boundary

Derek approved using Cap's dedicated Railway container as the execution boundary.
Bootstrap seeds a missing Codex config with `sandbox_mode = "danger-full-access"`
and `approval_policy = "on-request"`. Existing operator settings and login files
are preserved. This removes Codex's inner filesystem/network sandbox; commands
can access other task worktrees and credentials stored in Cap's container.
It does not grant GitHub access or provide a technical merge-approval gate.
Do not use this policy in a shared Hermes service.

## Native workflow wiring

`configure_workflow.py` performs a one-time, Cap-only migration using Hermes's
native board and Codex MCP APIs. It clones the four configured repositories,
creates matching boards, enables the Kanban toolset and gateway dispatcher,
and disables automatic review dispatch and decomposition. At most two workers
run at once. Existing display/model settings are preserved. `GH_TOKEN` must
already be supplied by the operator; setup does not create or modify secrets.

The image installs GitHub CLI. A narrow, build-checked adapter patch lets Cap's
Codex children inherit the intended GH_TOKEN and prevents Kanban from overriding
the explicitly approved container policy with Railway-incompatible workspace
sandboxing. Other service names retain the original behavior. The patch fails
the image build when its upstream anchors change.

Native MCP registration explicitly forwards task/run ownership and Slack routing
identifiers, not Slack/admin credentials. Re-run migration only after checking
the generated MCP environment allowlist remains present.

Review dispatch is off, but the supplied GitHub token has admin-level access.
The approval runbook is NOT GitHub-enforced branch protection or a credential
broker. No code here automatically merges PRs or enables GitHub auto-merge.

## Verification gates

- Set a unique ADMIN_PASSWORD for this service before exposing its dashboard.
- Sign Codex in using its own login flow. Do not copy existing agents' tokens.
- Configure Cap's separate Slack application; never reuse Atlas's bot tokens.
- Verify Codex can discover Kanban tools and a real native worker can create a
  worktree, execute commands, access GitHub, and move its own task to Review.
- Railway rejected Codex's namespace sandbox. Cap uses the approved
  container-level boundary above, not per-worktree OS isolation.
- Configure least-privilege GitHub access and an explicit merge-approval path.
  SOUL.md expresses the workflow but is not a technical merge-access control.
- Verify a real task, image attachment, preview, follow-up, restart recovery,
  and concurrent task isolation before inviting normal team requests.

Until those gates pass, existing Atlas threads continue through their existing
workflow. No DO resources or old worktrees are removed by this pilot.
# Mandatory Slack → native Kanban routing

Slack feedback: one board/task introduction per thread, then no repeated saved
messages. Reactions on each accepted request follow its own native run:
hourglass while queued, eyes while running, check mark when the answer is ready,
and X for a blocker/failure. Follow-ups waiting behind a running request remain
queued. Native short-handoff reactions are suppressed only for Cap's routed
events, including unmentioned thread replies. DMs retain normal Hermes behavior.
Reaction delivery state/retries live in `cap_feedback`; failures are logged with
an error ID and sanitized Slack error code and never prevent coding. The feedback
watcher runs independently of repository fetch/provisioning. Review/completion
notifications use the full summary from that specific run, delivered by the
existing native notifier with its retry/cursor behavior. No new model handles
notifications, and no token/env-var changes are required.

`routing.py` is installed as `gateway/cap_routing.py`. `patch_routing.py` wires it
into native gateway ingress and worker launch, with build-time anchor checks.
It applies only to Hermes-Cap and the four channels in `ROUTES`; normal DMs and
the Team/Owners services are unaffected. No optional plugin toggle can silently
disable this path. A failed import prevents dispatch rather than falling back
to an unregistered coding conversation.

The native board DB stores `cap_threads` (thread → task/path/branch/base SHA)
and `cap_messages` (durable request, dedup key and acknowledgement outbox).
Creation uses a per-board process lock and an atomic native operator-block
event; a bare `blocked` status is insufficient because Hermes recomputes it.
The router fetches origin/main and records its SHA without altering the primary
checkout, provisions a persistent worktree, subscribes Slack, then unblocks.
Worker launch verifies the mapped repository, branch and path. Native Hermes
retains ownership of claims, execution, retries, review and result delivery.

Mid-run follow-ups conservatively schedule another native turn after Review.
They are not live interrupts: changes already underway can finish first.
Duplicate inbound message IDs cannot create duplicate tasks or comments.
Acknowledgements are at-least-once across a Slack-send/DB-commit crash window;
result delivery remains the native notifier's responsibility. Neither worktrees
nor this routing layer restrict shell access outside the mapped repository.

Diagnostics: filter Hermes-Cap logs for `cap_request_saved`, `cap_task_ready`,
`cap_followup_ready`, `cap_route_failed`, `cap_ingress_failed` or
`cap_ack_delivery_failed`. Preparation failures remain blocked and expose the
same error ID in Slack and logs; failed delivery remains in the outbox. A
worker-blocked task requires `retry` after repair. Do not blindly reset tasks or
delete their branches/worktrees to recover. Closed tasks require a new thread.

`routing_smoke.py` exercises the installed native APIs in isolated temporary
boards and local Git fixtures; it sends no Slack messages and runs no model.
`import_pr15.py` is an explicit one-time operator migration, not a startup job.
