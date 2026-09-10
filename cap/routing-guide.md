## Cap's enforced Slack routing (v1)

This service uses the actual Codex CLI through Hermes's native codex_app_server
runtime. It is NOT OpenAI's separately hosted Slack integration and it does not
use a DigitalOcean helper.

For workspace TLREWAM8X, the gateway now intercepts these channels before the
ordinary chat agent runs:
- C0BV1CCCPS8 → gldnlab/vw-site → native Kanban board vw-site
- C0BUTAUA88M → gldnlab/vw-hq → native Kanban board vw-hq
- C0BUZL57K97 → gldnlab/vw-crm → native Kanban board vw-crm
- C0BUTATHDAR → gldnlab/vw-dashboards → native Kanban board vw-dashboards

The integration owns task creation and thread routing, not the model. Each
thread reuses its mapped task, branch and persistent worktree. Slack requests
and attachments are saved before dispatch. Follow-ups during a running task
are saved and cause another native worker turn after Review. Blocked workers
require an explicit `retry` after their blocker is fixed. Closed tasks require
a new Slack thread. DMs remain ordinary Hermes conversations.

Workers must read kanban_show and comments, follow the latest request, and
finish with kanban_request_review. For coding work, include checks, PR and the
verified preview URL or its blocker. Do not create another card for the same
thread. Proposals/questions do not authorize source edits. Never merge or
enable auto-merge without Derek approving the exact PR and commit.

PR #15's existing vw-site thread 1788976794.031949 is imported into the board;
its branch design/deep-mocha-textures is preserved, not replaced. The worktree
is now /data/cap/repos/vw-site/.worktrees/pr-15, not /tmp/vw-mocha.

The integration is installed in gateway/cap_routing.py. Its cap_threads and
cap_messages tables live inside each native board database on /data. Native
Kanban still owns workers, runs, claims and result notifications. This is
workflow enforcement, not an OS sandbox or GitHub merge-permission boundary.
