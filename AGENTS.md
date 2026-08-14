# AGENTS.md — notes for coding agents working on this fork

This is gldnlab's fork of praveen-ks-2001/hermes-agent-template. It deploys real Railway instances, so changes here redeploy live agents on push to `main`.

## What this fork adds over upstream

One patch, ~23 lines in `server.py`: multi-profile gateway supervision via the `HERMES_EXTRA_PROFILES` env var (comma-separated profile names). The admin server auto-starts, crash-restarts, and cleanly stops a gateway for each listed profile, same as the default profile.

- Do NOT drop this patch when merging upstream. After any merge, verify with: `grep -c HERMES_EXTRA_PROFILES server.py` (expect several hits) and `python3 -m py_compile server.py`.
- The patch is still necessary as of upstream v2026.8.3: the template has no multi-profile support, and Hermes's native per-profile supervision (s6-overlay / systemd) only exists in the official Docker image or bare-metal installs — not in this template's custom container, where `server.py` is the supervisor.

## Merging upstream

`git fetch upstream && git merge upstream/main` — upstream is https://github.com/praveen-ks-2001/hermes-agent-template. Merges have been clean so far. When the merge bumps `HERMES_REF` in the Dockerfile, re-check that the pip install extras still match upstream hermes-agent's `pyproject.toml`.

## Operational gotchas learned in production

- **Memory leaks come from MCP subprocesses, not Hermes itself.** MCP integrations (e.g. remote mail MCPs, `npx`-launched servers) each run as an `npm exec` → `sh` → `node` process tree of ~100–170 MB. Orphans can accumulate across agent sessions and grow the container by gigabytes over weeks (observed: 7 GB on a container whose healthy footprint is ~0.7 GB). A gateway restart reaps them. On metered hosts (Railway bills per GB-minute), consider a container memory cap so accumulation triggers a self-healing restart instead of a growing bill.
- Config, credentials, pairings, and memories all live on the `/data` volume — container restarts and redeploys never lose them.
- The admin dashboard is two layers: this template's login/setup/supervision wrapper, reverse-proxying Hermes's own native web dashboard (`hermes dashboard`, loopback :9119).
