# Atlas Slack → Codex router

This Hermes plugin makes Atlas the Slack identity for a persistent Codex coding
agent on DigitalOcean. Hermes remains the Slack gateway, but its normal model
loop does not handle messages in mapped coding channels.

```text
Slack channel + thread timestamp
              |
              v
      Hermes-Team / Atlas
              |
              v
 fixed SSH helper on DigitalOcean
              |
              v
 repo + worktree + branch + Codex thread
              |
              v
          GitHub PR
```

GitHub is the source of truth. SQLite stores only operational mappings and the
Codex thread ID needed to resume a conversation after restarts.

## Behavior

On the first authorized message in a configured channel, the plugin schedules a
background job and tells Hermes to skip normal dispatch. The helper then:

1. fetches the mapped repository from GitHub;
2. records the exact base SHA;
3. creates one `atlas/slack-*` branch and worktree;
4. starts `codex exec --json` with the configured model and reasoning effort in
   that worktree;
5. saves the emitted Codex thread ID in SQLite; and
6. returns Codex's final response through Atlas's existing Slack adapter.

Later messages in the same Slack thread reuse both the worktree and `codex exec
resume` session. No Buzz process or Buzz relay participates.

Codex runs with `approval_policy="never"` and the `workspace-write` sandbox. It
has network access so it can push its mapped branch and open a PR. Main-branch
protection and restricted GitHub credentials remain independent safeguards.
The deployed Atlas route pins `gpt-6-astra` with `medium` reasoning; the first
Slack status message and structured helper logs report both values.

## Failure behavior

- A mapped channel never falls back to Hermes's normal model.
- Workspace, Git, helper, Codex, timeout, session mismatch, and Slack delivery
  failures receive searchable error IDs.
- Errors are posted in the originating Slack thread when Slack is available.
- Matching structured records go to Railway logs and
  `/var/log/hermes-worktree-helper.jsonl` on DigitalOcean.
- If Slack delivery itself fails, that failure is still recorded in Railway.
- A second message while Codex is already running in the same thread fails
  explicitly instead of racing two writers.

## Configuration

The image includes the plugin, but it must be enabled only for the default
profile on the Hermes-Team Railway service:

```yaml
plugins:
  enabled:
    - slack-worktree-router
```

Set `HERMES_SLACK_WORKTREE_ROUTES` to the route file on the Hermes-Team volume.
Install the identical route file and helper code on DigitalOcean. Start with
`routes.example.yaml`, which contains the four Atlas channel mappings and two
required placeholders: the Slack workspace ID and Derek's Slack user ID.

Railway needs the fixed-helper SSH connection values:

- `HERMES_SLACK_WORKTREE_SSH_HOST`
- `HERMES_SLACK_WORKTREE_SSH_USER`
- `HERMES_SLACK_WORKTREE_SSH_PORT`
- `HERMES_SLACK_WORKTREE_SSH_KEY`
- `HERMES_SLACK_WORKTREE_KNOWN_HOSTS` (optional; defaults to the persistent
  `/data/.hermes/atlas-known-hosts`)

Do not put private keys, Slack tokens, GitHub tokens, or Codex auth files in the
repository or route file.

Before enabling the plugin, run:

```text
python /opt/hermes-agent/plugins/slack-worktree-router/diagnose.py
```

The diagnostic verifies the route fingerprint on both hosts and performs one
ephemeral Codex turn using the production `workspace-write` sandbox. Codex must
create and read a unique marker; the helper verifies and removes it and checks
that Git remains clean. This catches both expired refresh tokens and OS sandbox
failures that a text-only or read-only check would miss.

## Lifecycle

The DigitalOcean systemd timer runs cleanup daily. It removes a clean,
unchanged workspace after 24 hours of inactivity and removes clean workspaces
whose PR is merged. A closed-but-unmerged PR is removed only while its exact
branch head remains on GitHub. Workspaces with uncommitted changes, unpushed
commits, pushed work without a concluded PR, or an active Codex turn are
retained and logged.

Cleanup writes an archive tombstone before removing a worktree and local
branch. Replies to an archived Slack thread fail with an instruction to start a
new top-level thread instead of silently recreating conflicting state. An
untouched workspace can also be removed immediately by replying exactly
`@Atlas abandon this thread`; Atlas refuses the command if the workspace has
changes, commits, or a GitHub branch.
