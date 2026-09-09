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

GitHub is the source of truth for code and PRs. SQLite stores operational
mappings, Codex sessions, jobs, results, and Slack delivery receipts.

## Behavior

On each authorized message, the plugin saves an inbox row on Railway's volume
before telling Hermes to skip normal dispatch. A relay submits that message
with a stable ID (workspace + channel + message timestamp) over a short SSH
request. A separate systemd worker on DigitalOcean then:

1. fetches the mapped repository from GitHub;
2. records the exact base SHA;
3. creates one `atlas/slack-*` branch and worktree;
4. starts `codex exec --json` with the configured model and reasoning effort in
   that worktree;
5. saves the emitted Codex thread ID in SQLite; and
6. saves Codex's final response to the job database.

The profile's relay polls those saved results and posts through the same Atlas
Slack identity. It starts on gateway startup, scans unfinished inbox rows, and
retries interrupted submission or delivery. A single Slack status message is
updated to the final answer. A stable message metadata marker allows recovery
when Slack accepted a post but its acknowledgement was lost. The relay verifies
the Slack token's workspace and uses a file lock to prevent competing gateways
from delivering the same queue simultaneously.

Helper requests reuse an OpenSSH connection (60-second idle lifetime), with a
private, credential-specific socket and a bounded cross-process request lock.
Do not replace this with a new TCP connection per status poll: DigitalOcean's
SSH rate limit blocks that polling pattern. Keep the firewall limit enabled.

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
  `/srv/atlas/state/helper.jsonl` on DigitalOcean.
- If Slack delivery fails, the saved result remains pending and is retried.
- Messages in one thread execute FIFO; separate threads can run concurrently.
- SSH disconnects are transport failures, never proof that coding failed.
- Codex event streams are retained in `/srv/atlas/state/job-events/`. After a
  worker restart, a completed event stream recovers its result. An uncertain
  turn is marked interrupted and is not blindly replayed. Queued work survives.
- Gateway shutdown does not need to drain coding work: the systemd worker owns
  it independently. Slack delivery resumes when the gateway starts again.

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
commit a marker in a disposable worktree and the helper checks a guarded push
dry-run, then removes that worktree. The diagnostic also verifies the durable
worker is running. Restart/transport/Slack receipt recovery tests are in
`tests/test_slack_worktree_router.py`.

For a read-only transport check, run `diagnose.py --transport-only`. This checks
connection reuse across 13 requests over more than 36 seconds, spanning the
SSH firewall's rate-limit window, without starting Codex or creating a worktree.

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
