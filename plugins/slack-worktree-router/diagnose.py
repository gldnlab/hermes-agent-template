#!/usr/bin/env python3
"""Fail-fast deployment diagnostics for Atlas's workspace router."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from router import Router, config_fingerprint


def main() -> int:
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
            preflight = router._remote_request(config, "codex_preflight")
            preflight_matches = (
                preflight.get("authenticated") is True
                and preflight.get("model") == config.codex_model
                and preflight.get("reasoning_effort") == config.codex_reasoning_effort
            )
            check(
                "codex_auth",
                preflight_matches,
                f"fresh read-only {config.codex_model}/{config.codex_reasoning_effort} turn succeeded",
            )
    except Exception as exc:
        check("diagnostic_exception", False, f"{type(exc).__name__}: {exc}")

    ok = all(bool(item["ok"]) for item in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
