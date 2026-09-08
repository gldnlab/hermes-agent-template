# DigitalOcean helper deployment

The Railway plugin uses a dedicated control SSH credential to call one forced
command on the coding host. Hermes never receives an unrestricted shell.

Install these plugin files under `/opt/hermes-worktree-router/`:

- `router.py`
- `remote_helper.py`
- `cleanup.py`

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

Then run the `codex_preflight` operation. It performs one ephemeral read-only
turn and fails on an expired or invalid refresh token:

```text
printf '%s' '{"version":1,"request_id":"codex-check","operation":"codex_preflight"}' |
  ssh -o BatchMode=yes -i /path/to/control-key atlas-control@coding-host
```
