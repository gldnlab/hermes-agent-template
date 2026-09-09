#!/usr/bin/env python3
"""Fail-fast deployment diagnostics for Atlas's workspace router."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from router import Router, config_fingerprint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport-only', action='store_true',
                        help='exercise repeated SSH polls without running Codex or creating a worktree')
    args = parser.parse_args()
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        router = Router()
        config = router.config()
        check("routes", bool(config.routes), f"{len(config.routes)} configured channel route(s)")
        check("workspace_backend", config.backend == "ssh", f"configured as {config.backend}")
        check("ssh_client", shutil.which("ssh") is not None, "ssh executable on Railway image")
        check("control_key", bool(config.ssh_key) and Path(config.ssh_key).is_file(),
              "configured control key path exists")

        if config.backend == "ssh":
            response = router._remote_request(config, "health")
            check(
                "helper_health",
                response.get("backend") == "local" and int(response.get("routes", 0)) > 0,
                f"helper reports backend={response.get('backend')} routes={response.get('routes')}",
            )
            check(
                "route_config_match",
                response.get("config_fingerprint") == config_fingerprint(config),
                "Railway and DigitalOcean route configuration fingerprints match",
            )
            check("codex_binary", bool(response.get("codex_binary_present")),
                  "Codex binary exists on DigitalOcean")
            check("codex_home", bool(response.get("codex_home_present")),
                  "configured Codex home exists on DigitalOcean")
            check('durable_worker', response.get('job_protocol') == 1
                  and response.get('worker_running') is True,
                  'DigitalOcean durable job worker holds its supervision lock')
            check(
                "codex_model",
                response.get("codex_model") == config.codex_model,
                f"helper pins {response.get('codex_model')}",
            )
            check(
                "codex_reasoning_effort",
                response.get("codex_reasoning_effort") == config.codex_reasoning_effort,
                f"helper pins {response.get('codex_reasoning_effort')} reasoning",
            )
            if args.transport_only:
                socket = Path(next(arg.split('=', 1)[1] for arg in router._ssh_argv(config)
                                   if arg.startswith('ControlPath=')))
                inodes = {socket.stat().st_ino}
                for _ in range(12):
                    time.sleep(3)
                    poll = router._remote_request(config, 'health')
                    if poll.get('config_fingerprint') != config_fingerprint(config):
                        raise RuntimeError('helper identity changed during repeated SSH checks')
                    inodes.add(socket.stat().st_ino)
                check('ssh_poll_reuse', len(inodes) == 1,
                      '13 helper requests over 36+ seconds used one SSH connection socket')
                ok = all(bool(item['ok']) for item in checks)
                print(json.dumps({'ok': ok, 'checks': checks}, indent=2, sort_keys=True))
                return 0 if ok else 1
            preflight = router._remote_request(config, "codex_preflight")
            preflight_matches = (
                preflight.get("authenticated") is True
                and preflight.get("workspace_write") is True
                and preflight.get("git_commit") is True
                and preflight.get("push_dry_run") is True
                and preflight.get("model") == config.codex_model
                and preflight.get("reasoning_effort") == config.codex_reasoning_effort
            )
            check(
                "codex_auth",
                preflight_matches,
                f"fresh workspace-write {config.codex_model}/{config.codex_reasoning_effort} turn succeeded",
            )
    except Exception as exc:
        check("diagnostic_exception", False, f"{type(exc).__name__}: {exc}")

    ok = all(bool(item["ok"]) for item in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
