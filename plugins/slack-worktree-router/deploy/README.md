# DigitalOcean helper deployment

The Railway plugin uses a dedicated control SSH credential to call one forced
command on the coding host. Hermes never receives an unrestricted shell.

Install these plugin files under `/opt/hermes-worktree-router/`:

- `router.py`
- `remote_helper.py`
- `cleanup.py`
- `jobs.py`

Install `deploy/atlas-pre-push` as
`/opt/hermes-worktree-router/git-hooks/pre-push`, root-owned and executable.
Every dedicated Atlas checkout must set:

```text
git config core.hooksPath /opt/hermes-worktree-router/git-hooks
```

The helper refuses to provision or run Codex if this guard is absent or a
checkout points elsewhere. The guard permits pushes only to `atlas/*` branches.

Install PyYAML for `/usr/bin/python3`. Install the
`deploy/hermes-worktree-helper` wrapper as
`/usr/local/bin/hermes-worktree-helper`, owned by root and not writable by its
runtime user. Install the populated route configuration as
`/etc/hermes/slack-worktree-routes.yaml`.

Ubuntu hosts with `kernel.apparmor_restrict_unprivileged_userns=1` must also
install `deploy/codex-bwrap.apparmor` under `/etc/apparmor.d/codex-bwrap` and
load it with `apparmor_parser -r /etc/apparmor.d/codex-bwrap`. This grants the
`userns` permission only to Codex's bundled bubblewrap executable; do not
disable AppArmor's global user-namespace restriction. Revalidate the profile
path whenever the Codex package layout changes.

Pre-create the operational paths with private permissions:

```text
/srv/atlas/state/
/srv/atlas/repos/
/srv/atlas/worktrees/
/var/log/hermes-worktree-helper.jsonl
```

The `atlas` helper account owns `/srv/atlas`, including the repositories,
worktrees, state, GitHub auth, and Codex home. It is not a sudoer. The route
file is root-owned, group-readable by `atlas`, and not writable by that account.
The helper executable and pre-push guard are root-owned and not writable by
`atlas`.

Install `deploy/atlas-worker.service` under `/etc/systemd/system/`, then run
`systemctl daemon-reload` and `systemctl enable --now atlas-worker.service`.
The worker owns execution independently of SSH. Its existing credential file
is loaded by systemd; no new secret or production environment variable is
required. Deploy the worker before the Railway plugin. Restart it only when
idle where possible; uncertain running turns are marked interrupted on startup
and are not automatically replayed. Queued jobs remain queued. Inspect
`journalctl -u atlas-worker` and `/srv/atlas/state/helper.jsonl` for job IDs.

The Railway relay starts only when the plugin is enabled in a gateway process.
Its inbox and delivery receipts live in
`/data/.hermes/atlas/atlas-delivery.sqlite3` on the persistent volume (resolved
through the profile's Hermes home, not the remote `state_db` path). The profile's Slack
configuration supplies its token; named profiles without this plugin are not
used. Test messages should be explicitly authorized before live Slack checks.

Install `deploy/atlas-worktree-cleanup.service` and
`deploy/atlas-worktree-cleanup.timer` under `/etc/systemd/system/`, then run:

```text
systemctl daemon-reload
systemctl enable --now atlas-worktree-cleanup.timer
```

The timer runs daily as the unprivileged `atlas` user. It expires untouched
workspaces after 24 hours and safely handles concluded PRs. Cleanup records are
written to `/srv/atlas/state/helper.jsonl` and the systemd journal.

If Codex needs a GitHub token, put only the required runtime values in
`/etc/hermes/atlas.env`, owned by root and mode `0600`. The wrapper sources that
file without printing it. Do not reuse or source a Buzz service environment.

Restrict the control public key in that account's `authorized_keys`:

```text
restrict,command="/usr/local/bin/hermes-worktree-helper" ssh-ed25519 AAAA... atlas-worktree-control
```

The forced command means the control key exposes only the helper's versioned
JSON operations, not an arbitrary shell. One of those operations launches
Codex inside a previously verified mapped worktree; it cannot choose an
unregistered path. The helper accepts one request on stdin and returns one JSON
response on stdout. It logs matching request and error IDs to stderr (which
Railway records) and to `/srv/atlas/state/helper.jsonl`.

Before enabling the Railway plugin, test the forced-command path with the same
key it will use:

```text
printf '%s' '{"version":1,"request_id":"health-check","operation":"health"}' |
  ssh -o BatchMode=yes -i /path/to/control-key atlas-control@coding-host
```

The response must be one JSON object with `"ok":true`, and the same
`health-check` ID must appear in the host log.

Then run the `codex_preflight` operation. It creates a disposable worktree and
branch, performs one ephemeral `workspace-write` turn, verifies that Codex can
write and commit a marker, checks a guarded GitHub push with `--dry-run`, and
removes the worktree and branch. It fails if authentication, the OS sandbox,
Git metadata access, or push authorization is broken:

```text
printf '%s' '{"version":1,"request_id":"codex-check","operation":"codex_preflight"}' |
  ssh -o BatchMode=yes -i /path/to/control-key atlas-control@coding-host
```
