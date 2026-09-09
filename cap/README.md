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
login directory. Workspaces live under `/data/cap`. Nothing is copied
from an existing agent's volume or environment.

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
