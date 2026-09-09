# Verified native Cap workflow

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
