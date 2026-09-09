## Cap's connected coding workflow

Use the native Hermes Kanban tools exposed by hermes-tools MCP. Do not build a
second task system. The default Hermes profile is named `default` and is Cap.

Repository routes (one native Kanban board per repository):
- board `vw-site`: gldnlab/vw-site, /data/cap/repos/vw-site
- board `vw-hq`: gldnlab/vw-hq, /data/cap/repos/vw-hq
- board `vw-crm`: gldnlab/vw-crm, /data/cap/repos/vw-crm
- board `vw-dashboards`: gldnlab/vw-dashboards, /data/cap/repos/vw-dashboards

For a coding request, clarify acceptance criteria only when necessary, then
create the task on the matching board, assigned to `default`, with
workspace_kind `worktree` and workspace_path pointing to the repository above.
Before creating a new task, fetch origin and fast-forward the clean primary
checkout to origin/main. Never reset or discard changes. If it isn't clean,
stop and report the problem. Never do task edits in the primary checkout.
Hermes creates a separate linked worktree and branch for each task.
Keep the task ID in the Slack conversation. Follow-ups belong on that task;
do not create a fresh task for every message. Use native Kanban comments and
notifications so the board and Slack retain the result and blockers.

Workers: read the task using kanban_show, implement only its approved scope,
run relevant checks and review the diff. GitHub CLI `gh` is installed and
authenticated through the service's GH_TOKEN. Never print credentials.
Commit and push only the task branch, then open a PR. Use GitHub PR checks and
deployment statuses to locate and verify the actual preview URL. Never invent
a preview, repurpose the production URL, or claim a preview exists from a build
alone. If preview authentication or deployment setup blocks verification,
record that blocker explicitly and keep the task open.

When ready, use kanban_request_review with the PR URL, verified preview URL (or
an explicit preview blocker), changes, tests, and review findings. Do not call
kanban_complete for code awaiting Derek's review. Automatic review dispatch is
disabled: leave the card in Review until Derek explicitly approves the exact
PR/commit. Never merge or enable auto-merge merely because checks pass or the
task was requested. Ask again if new commits are added after approval.

An explicit request for a proposal does not authorize implementation. Submit
the proposal and wait. For infrastructure verification tasks, obey their
no-push/no-PR scope and report the verified result to Review.
