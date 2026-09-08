#!/usr/bin/env python3
"""DigitalOcean control-plane helper for the Slack worktree router.

The helper accepts exactly one JSON request on stdin and writes exactly one
JSON response on stdout. Human/diagnostic output is JSON on stderr so SSH
callers never have to scrape prose. It deliberately exposes no arbitrary
command execution surface.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from router import (
    FORCE_LOCAL_ENV,
    ROUTES_ENV,
    Mapping,
    Router,
    RouterError,
    config_fingerprint,
    safe_detail,
)


MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_LOG_PATH = "/var/log/hermes-worktree-helper.jsonl"
MAX_CODEX_OUTPUT_BYTES = 8 * 1024 * 1024


def _log(event: str, **fields: Any) -> None:
    record = {
        "component": "slack-worktree-helper",
        "event": event,
        **{
            key: safe_detail(value) if isinstance(value, str) else value
            for key, value in fields.items()
        },
    }
    line = json.dumps(record, sort_keys=True, default=str)
    print(line, file=sys.stderr, flush=True)
    path = os.environ.get("HERMES_SLACK_WORKTREE_HELPER_LOG", DEFAULT_LOG_PATH).strip()
    if not path:
        return
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            os.write(fd, f"{line}\n".encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except OSError:
        # stderr is still captured into Railway logs; inability to open the
        # secondary host log must never corrupt the JSON protocol on stdout.
        return


def _required(request: dict[str, Any], name: str) -> str:
    value = str(request.get(name) or "").strip()
    if not value:
        raise RouterError(f"request is missing {name}")
    return value


def _codex_session(router: Router, mapping: Mapping) -> str | None:
    config = router.config()
    with router._connect(config) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_sessions (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                codex_thread_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (workspace_id, channel_id, thread_ts)
            )
            """
        )
        row = conn.execute(
            """
            SELECT codex_thread_id FROM codex_sessions
            WHERE workspace_id=? AND channel_id=? AND thread_ts=?
            """,
            (mapping.workspace_id, mapping.channel_id, mapping.thread_ts),
        ).fetchone()
    return str(row["codex_thread_id"]) if row else None


def _save_codex_session(router: Router, mapping: Mapping, thread_id: str) -> None:
    config = router.config()
    with router._connect(config) as conn:
        conn.execute(
            """
            INSERT INTO codex_sessions (
                workspace_id, channel_id, thread_ts, codex_thread_id
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id, channel_id, thread_ts) DO UPDATE SET
                codex_thread_id=excluded.codex_thread_id,
                updated_at=CURRENT_TIMESTAMP
            """,
            (mapping.workspace_id, mapping.channel_id, mapping.thread_ts, thread_id),
        )


def _parse_codex_jsonl(stdout: str) -> tuple[str, str]:
    thread_id = ""
    final = ""
    failure = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                final = str(item.get("text") or "").strip()
        elif event_type in {"error", "turn.failed"}:
            raw = event.get("error") if event_type == "turn.failed" else event.get("message")
            if isinstance(raw, dict):
                raw = raw.get("message")
            failure = str(raw or failure)
    if failure:
        raise RouterError(f"Codex turn failed: {safe_detail(failure, 1000)}")
    if not thread_id:
        raise RouterError("Codex emitted no thread.started event")
    if not final:
        raise RouterError("Codex emitted no final agent message")
    return thread_id, final


def _run_codex(router: Router, mapping: Mapping, prompt: str) -> dict[str, Any]:
    config = router.config()
    router.verify(mapping, config)
    if not config.codex_binary or not Path(config.codex_binary).is_file():
        raise RouterError(f"Codex binary is missing: {config.codex_binary}")
    if not config.codex_home.is_dir():
        raise RouterError(f"Codex home is missing: {config.codex_home}")

    lock_dir = config.state_db.parent / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_key = f"{mapping.workspace_id}:{mapping.channel_id}:{mapping.thread_ts}".encode()
    lock_name = f"codex-{hashlib.sha256(lock_key).hexdigest()[:16]}.lock"
    lock_path = lock_dir / lock_name
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RouterError("Atlas is already handling another message in this Slack thread") from exc

        prior_thread_id = _codex_session(router, mapping)
        common = [
            "--json",
            "-c", 'approval_policy="never"',
            "-c", f'sandbox_mode="{config.codex_sandbox}"',
            "-c", "sandbox_workspace_write.network_access=true",
        ]
        if prior_thread_id:
            argv = [
                config.codex_binary, "exec", "resume", *common,
                prior_thread_id, "-",
            ]
        else:
            argv = [
                config.codex_binary, "exec", *common,
                "--color", "never",
                "--sandbox", config.codex_sandbox,
                "-C", str(mapping.worktree), "-",
            ]
        env = os.environ.copy()
        env["CODEX_HOME"] = str(config.codex_home)
        _log(
            "codex_started",
            session_id=mapping.session_id,
            github_repo=mapping.github_repo,
            worktree=str(mapping.worktree),
            resumed=bool(prior_thread_id),
        )
        try:
            result = subprocess.run(
                argv,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=mapping.worktree,
                env=env,
                timeout=config.codex_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RouterError(
                f"Codex exceeded its {config.codex_timeout}-second turn timeout"
            ) from exc
        if len(result.stdout.encode("utf-8", errors="replace")) > MAX_CODEX_OUTPUT_BYTES:
            raise RouterError("Codex JSON event stream exceeded 8 MiB")
        stderr_tail = safe_detail(result.stderr.strip()[-4000:], 4000)
        if result.returncode:
            detail = stderr_tail or safe_detail(result.stdout.strip()[-2000:], 2000)
            raise RouterError(f"Codex exited {result.returncode}: {detail}")
        thread_id, final = _parse_codex_jsonl(result.stdout)
        if prior_thread_id and thread_id != prior_thread_id:
            raise RouterError("Codex resumed a different thread than the persisted Slack mapping")
        _save_codex_session(router, mapping, thread_id)
        _log(
            "codex_succeeded",
            session_id=mapping.session_id,
            codex_thread_id=thread_id,
            resumed=bool(prior_thread_id),
        )
        return {
            "codex_thread_id": thread_id,
            "final": final,
            "resumed": bool(prior_thread_id),
        }


def _codex_preflight(router: Router) -> dict[str, Any]:
    config = router.config()
    route = next(iter(config.routes.values()))
    router._assert_repo(route, config)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(config.codex_home)
    argv = [
        config.codex_binary, "exec", "--ephemeral", "--json", "--color", "never",
        "--sandbox", "read-only", "-C", str(route.repo),
        "-c", 'approval_policy="never"', "-",
    ]
    try:
        result = subprocess.run(
            argv,
            input="Reply with exactly: ATLAS_CODEX_OK",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=route.repo,
            env=env,
            timeout=min(config.helper_timeout - 5, 120),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RouterError("Codex authentication preflight timed out") from exc
    if result.returncode:
        detail = safe_detail(result.stderr.strip()[-3000:] or result.stdout.strip()[-2000:], 3000)
        raise RouterError(f"Codex authentication preflight failed: {detail}")
    _thread_id, final = _parse_codex_jsonl(result.stdout)
    if final.strip() != "ATLAS_CODEX_OK":
        raise RouterError("Codex authentication preflight returned an unexpected response")
    return {"authenticated": True}


def _dispatch(router: Router, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("version") != 1:
        raise RouterError("unsupported request version")
    operation = _required(request, "operation")

    if operation == "health":
        config = router.config()
        return {
            "backend": config.backend,
            "routes": len(config.routes),
            "config_fingerprint": config_fingerprint(config),
            "codex_binary_present": Path(config.codex_binary).is_file(),
            "codex_home_present": config.codex_home.is_dir(),
        }

    if operation == "codex_preflight":
        return _codex_preflight(router)

    if operation == "lookup":
        mapping = router.lookup(_required(request, "session_id"))
        return {"mapping": mapping.to_dict() if mapping else None}

    if operation == "provision":
        mapping = router.provision(
            session_id=_required(request, "session_id"),
            workspace_id=_required(request, "workspace_id"),
            channel_id=_required(request, "channel_id"),
            thread_ts=_required(request, "thread_ts"),
            user_id=_required(request, "user_id"),
        )
        return {"mapping": mapping.to_dict()}

    if operation == "verify":
        session_id = _required(request, "session_id")
        mapping = router.lookup(session_id)
        if mapping is None:
            raise RouterError("session has no registered workspace")
        expected = request.get("expected")
        if not isinstance(expected, dict) or Mapping.from_dict(expected) != mapping:
            raise RouterError("caller mapping does not match the persisted workspace")
        router.verify(mapping)
        return {"mapping": mapping.to_dict()}

    if operation == "resolve_path":
        session_id = _required(request, "session_id")
        mapping = router.lookup(session_id)
        if mapping is None:
            raise RouterError("session has no registered workspace")
        router.verify(mapping)
        raw_path = str(request.get("path") or ".")
        resolved = router._inside_local(mapping.worktree, raw_path)
        return {"path": str(resolved)}

    if operation == "codex_run":
        session_id = _required(request, "session_id")
        prompt = _required(request, "prompt")
        mapping = router.lookup(session_id)
        if mapping is None:
            raise RouterError("session has no registered workspace")
        expected = request.get("expected")
        if not isinstance(expected, dict) or Mapping.from_dict(expected) != mapping:
            raise RouterError("caller mapping does not match the persisted workspace")
        return _run_codex(router, mapping, prompt)

    raise RouterError(f"unsupported operation: {operation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to the shared route configuration")
    args = parser.parse_args(argv)
    if args.config:
        os.environ[ROUTES_ENV] = str(Path(args.config).expanduser().resolve())
    os.environ[FORCE_LOCAL_ENV] = "1"
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    request_id = uuid.uuid4().hex[:12]
    if len(raw) > MAX_REQUEST_BYTES:
        response = {
            "ok": False,
            "request_id": request_id,
            "error_id": request_id,
            "error": "request exceeds 64 KiB",
        }
        print(json.dumps(response, separators=(",", ":")), flush=True)
        _log("request_rejected", request_id=request_id, error="request too large")
        return 2

    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise RouterError("request must be a JSON object")
        request_id = str(request.get("request_id") or request_id)
        if not request_id or len(request_id) > 64:
            raise RouterError("invalid request_id")
        operation = str(request.get("operation") or "")
        _log("request_started", request_id=request_id, operation=operation)
        result = _dispatch(Router(), request)
        response = {"ok": True, "request_id": request_id, **result}
        print(json.dumps(response, separators=(",", ":"), default=str), flush=True)
        _log("request_succeeded", request_id=request_id, operation=operation)
        return 0
    except Exception as exc:
        error_id = getattr(exc, "error_id", uuid.uuid4().hex[:12])
        response = {
            "ok": False,
            "request_id": request_id,
            "error_id": error_id,
            "error": safe_detail(exc),
        }
        print(json.dumps(response, separators=(",", ":")), flush=True)
        _log(
            "request_failed",
            request_id=request_id,
            error_id=error_id,
            error_type=type(exc).__name__,
            error=safe_detail(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
