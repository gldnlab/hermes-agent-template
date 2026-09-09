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
already sets HOME=/data); workspaces live under `/data/cap`. Nothing is copied
from an existing agent's volume or environment.

## Pilot gates — not yet a working coding deployment

- Set a unique ADMIN_PASSWORD for this service before exposing its dashboard.
- Sign Codex in using its own login flow. Do not copy existing agents' tokens.
- Configure Cap's separate Slack application; never reuse Atlas's bot tokens.
- Native Codex runtime is selected; native Kanban dispatch remains off until
  execution and human-review behavior have been verified.
- Railway rejected Codex's namespace sandbox in the Hermes-Team probe. The
  dedicated-service proposal uses container-level isolation, not per-worktree
  OS isolation. Do not enable broad permissions in a shared Hermes service.
- The installed Hermes version overrides Kanban worker sandbox settings with
  workspace-write. Resolve and test this compatibility issue before enabling
  worker dispatch; do not mistake the selected runtime for a successful test.
- Configure least-privilege GitHub access and an explicit merge-approval path.
  SOUL.md expresses the workflow but is not a technical merge-access control.
- Verify a real task, image attachment, preview, follow-up, restart recovery,
  and concurrent task isolation before inviting normal team requests.

Until those gates pass, existing Atlas threads continue through their existing
workflow. No DO resources or old worktrees are removed by this pilot.
