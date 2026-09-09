"""Deterministic Slack-thread to Git-worktree routing for Hermes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


PLUGIN_NAME = "slack-worktree-router"
ROUTES_ENV = "HERMES_SLACK_WORKTREE_ROUTES"
FORCE_LOCAL_ENV = "HERMES_SLACK_WORKTREE_FORCE_LOCAL"
logger = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_PATCH_PATH = re.compile(
    r"^(\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*)(.+)$", re.MULTILINE
)
_PATCH_MOVE = re.compile(
    r"^(\*\*\*\s*Move\s+File:\s*)(.+?)\s*->\s*(.+)$", re.MULTILINE
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_LIKELY_TOKEN = re.compile(
    r"(?i)\b(?:github_pat_|gh[pousr]_|xox[baprs]-|sk-)[A-Za-z0-9_-]{8,}"
)
_AUTH_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+")
_SSH_RUNTIME_ROOT = Path('/tmp')

FILE_TOOLS = {"read_file", "write_file", "search_files"}
PATCH_TOOL = "patch"
TERMINAL_TOOL = "terminal"
UNCONFINED_TOOLS = {"execute_code", "delegate_task"}
PROTECTED_TOOLS = FILE_TOOLS | {PATCH_TOOL, TERMINAL_TOOL} | UNCONFINED_TOOLS


class RouterError(RuntimeError):
    """A fail-closed routing or isolation error."""

    def __init__(self, message: str, *, error_id: str | None = None):
        super().__init__(message)
        self.error_id = error_id or uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Route:
    workspace_id: str
    channel_id: str
    repo: Path
    github_repo: str
    base_branch: str
    allowed_users: frozenset[str]


@dataclass(frozen=True)
class Mapping:
    session_id: str
    workspace_id: str
    channel_id: str
    thread_ts: str
    repo: Path
    github_repo: str
    worktree: Path
    branch: str
    base_branch: str
    base_sha: str

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "repo": str(self.repo),
            "github_repo": self.github_repo,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Mapping":
        try:
            return cls(
                session_id=str(value["session_id"]),
                workspace_id=str(value["workspace_id"]),
                channel_id=str(value["channel_id"]),
                thread_ts=str(value["thread_ts"]),
                repo=Path(str(value["repo"])),
                github_repo=str(value["github_repo"]),
                worktree=Path(str(value["worktree"])),
                branch=str(value["branch"]),
                base_branch=str(value["base_branch"]),
                base_sha=str(value["base_sha"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RouterError(f"invalid workspace-helper mapping: {exc}") from exc


@dataclass(frozen=True)
class Config:
    state_db: Path
    worktrees_root: Path
    git_hooks_path: Path
    branch_prefix: str
    deny_unmapped: bool
    terminal_isolation: str
    terminal_image: str
    terminal_network: str
    terminal_pass_env: tuple[str, ...]
    backend: str
    ssh_host: str
    ssh_user: str
    ssh_port: int
    ssh_key: str
    helper_command: str
    helper_timeout: int
    codex_binary: str
    codex_home: Path
    codex_model: str
    codex_reasoning_effort: str
    codex_sandbox: str
    codex_timeout: int
    routes: dict[str, Route]


def safe_detail(value: Any, limit: int = 2000) -> str:
    text = str(value or "")
    text = _URL_USERINFO.sub(r"\1***@", text)
    text = _LIKELY_TOKEN.sub("[REDACTED]", text)
    text = _AUTH_VALUE.sub(r"\1[REDACTED]", text)
    return text[:limit]


def log_event(level: int, event: str, **fields: Any) -> None:
    """Emit one searchable JSON record to Railway or helper stderr logs."""
    safe = {"component": PLUGIN_NAME, "event": event}
    safe.update({
        key: safe_detail(value) if isinstance(value, str) else value
        for key, value in fields.items()
        if value is not None
    })
    logger.log(level, json.dumps(safe, sort_keys=True, default=str))


def config_fingerprint(config: Config) -> str:
    payload = {
        "state_db": str(config.state_db),
        "worktrees_root": str(config.worktrees_root),
        "git_hooks_path": str(config.git_hooks_path),
        "branch_prefix": config.branch_prefix,
        "codex": {
            "binary": config.codex_binary,
            "home": str(config.codex_home),
            "model": config.codex_model,
            "reasoning_effort": config.codex_reasoning_effort,
            "sandbox": config.codex_sandbox,
            "timeout": config.codex_timeout,
        },
        "routes": {
            channel: {
                "workspace_id": route.workspace_id,
                "repo": str(route.repo),
                "github_repo": route.github_repo,
                "base_branch": route.base_branch,
                "allowed_users": sorted(route.allowed_users),
            }
            for channel, route in sorted(config.routes.items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _run_git(*args: str, cwd: Path | None = None) -> str:
    command = ["git"]
    if cwd is not None:
        command.extend(["-C", str(cwd)])
    command.extend(args)
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:800]
        raise RouterError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RouterError(f"missing required configuration: {label}")
    return text


def _load_config() -> Config:
    raw_path = os.environ.get(ROUTES_ENV, "").strip()
    if not raw_path:
        raise RouterError(f"{ROUTES_ENV} is not set")
    path = Path(raw_path).expanduser().resolve()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise RouterError(f"cannot load routes file {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RouterError("routes file must be a mapping with version: 1")

    routes_raw = payload.get("slack_repo_routes")
    if not isinstance(routes_raw, dict) or not routes_raw:
        raise RouterError("slack_repo_routes must contain at least one channel")

    routes: dict[str, Route] = {}
    for channel, value in routes_raw.items():
        channel_id = _required_text(channel, "channel id")
        if not _SAFE_ID.fullmatch(channel_id):
            raise RouterError(f"invalid Slack channel id: {channel_id!r}")
        if not isinstance(value, dict):
            raise RouterError(f"route {channel_id} must be a mapping")
        workspace_id = _required_text(value.get("workspace_id"), f"{channel_id}.workspace_id")
        if not _SAFE_ID.fullmatch(workspace_id):
            raise RouterError(f"invalid Slack workspace id: {workspace_id!r}")
        allowed = frozenset(
            str(item).strip()
            for item in (value.get("allowed_users") or [])
            if str(item).strip()
        )
        if not allowed:
            raise RouterError(f"route {channel_id} requires allowed_users")
        repo = Path(_required_text(value.get("repo"), f"{channel_id}.repo")).expanduser().resolve()
        routes[channel_id] = Route(
            workspace_id=workspace_id,
            channel_id=channel_id,
            repo=repo,
            github_repo=_required_text(value.get("github_repo"), f"{channel_id}.github_repo"),
            base_branch=_required_text(value.get("base_branch", "main"), f"{channel_id}.base_branch"),
            allowed_users=allowed,
        )

    terminal = payload.get("terminal") or {}
    if not isinstance(terminal, dict):
        raise RouterError("terminal must be a mapping")
    isolation = str(terminal.get("isolation", "docker")).strip().lower()
    if isolation not in {"docker", "workdir"}:
        raise RouterError("terminal.isolation must be docker or workdir")
    image = str(terminal.get("image", "")).strip()
    if isolation == "docker" and not image:
        raise RouterError("terminal.image is required for docker isolation")
    network = str(terminal.get("network", "bridge")).strip()
    if not _SAFE_ID.fullmatch(network):
        raise RouterError("terminal.network contains unsafe characters")
    pass_env = tuple(str(item).strip() for item in terminal.get("pass_env", []) if str(item).strip())
    for name in pass_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RouterError(f"invalid terminal.pass_env name: {name!r}")

    backend_raw = payload.get("workspace_backend") or {}
    if not isinstance(backend_raw, dict):
        raise RouterError("workspace_backend must be a mapping")
    backend = str(backend_raw.get("type", "local")).strip().lower()
    if os.environ.get(FORCE_LOCAL_ENV, "").lower() in {"1", "true", "yes"}:
        backend = "local"
    if backend not in {"local", "ssh"}:
        raise RouterError("workspace_backend.type must be local or ssh")
    ssh_host = str(
        os.environ.get("HERMES_SLACK_WORKTREE_SSH_HOST")
        or os.environ.get("TERMINAL_SSH_HOST")
        or backend_raw.get("host", "")
    ).strip()
    ssh_user = str(
        os.environ.get("HERMES_SLACK_WORKTREE_SSH_USER")
        or os.environ.get("TERMINAL_SSH_USER")
        or backend_raw.get("user", "")
    ).strip()
    ssh_key = str(
        os.environ.get("HERMES_SLACK_WORKTREE_SSH_KEY")
        or os.environ.get("TERMINAL_SSH_KEY")
        or backend_raw.get("key", "")
    ).strip()
    try:
        ssh_port = int(
            os.environ.get("HERMES_SLACK_WORKTREE_SSH_PORT")
            or os.environ.get("TERMINAL_SSH_PORT")
            or backend_raw.get("port", 22)
        )
        helper_timeout = int(backend_raw.get("timeout", 150))
    except (TypeError, ValueError) as exc:
        raise RouterError("workspace_backend port and timeout must be integers") from exc
    if not 1 <= ssh_port <= 65535:
        raise RouterError("workspace_backend port is out of range")
    if not 5 <= helper_timeout <= 600:
        raise RouterError("workspace_backend timeout must be between 5 and 600 seconds")
    helper_command = str(
        backend_raw.get("helper_command", "/usr/local/bin/hermes-worktree-helper")
    ).strip()
    if backend == "ssh":
        if not ssh_host or not ssh_user:
            raise RouterError(
                "SSH workspace backend requires HERMES_SLACK_WORKTREE_SSH_HOST "
                "and HERMES_SLACK_WORKTREE_SSH_USER (or TERMINAL_SSH_* fallbacks)"
            )
        if not helper_command.startswith("/") or any(char.isspace() for char in helper_command):
            raise RouterError("workspace_backend.helper_command must be one absolute path")

    codex_raw = payload.get("codex") or {}
    if not isinstance(codex_raw, dict):
        raise RouterError("codex must be a mapping")
    codex_binary = str(codex_raw.get("binary", "/usr/bin/codex")).strip()
    if not codex_binary.startswith("/") or any(char.isspace() for char in codex_binary):
        raise RouterError("codex.binary must be one absolute path")
    codex_home = Path(
        _required_text(codex_raw.get("home"), "codex.home")
    ).expanduser().resolve()
    codex_model = _required_text(codex_raw.get("model"), "codex.model")
    if not _SAFE_ID.fullmatch(codex_model):
        raise RouterError("codex.model contains unsafe characters")
    codex_reasoning_effort = _required_text(
        codex_raw.get("reasoning_effort"), "codex.reasoning_effort"
    ).lower()
    if codex_reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise RouterError(
            "codex.reasoning_effort must be minimal, low, medium, high, or xhigh"
        )
    codex_sandbox = str(codex_raw.get("sandbox", "workspace-write")).strip()
    if codex_sandbox not in {"read-only", "workspace-write"}:
        raise RouterError("codex.sandbox must be read-only or workspace-write")
    try:
        codex_timeout = int(codex_raw.get("timeout", 3600))
    except (TypeError, ValueError) as exc:
        raise RouterError("codex.timeout must be an integer") from exc
    if not 60 <= codex_timeout <= 14400:
        raise RouterError("codex.timeout must be between 60 and 14400 seconds")

    state_db = Path(
        _required_text(payload.get("state_db"), "state_db")
    ).expanduser().resolve()
    root = Path(
        _required_text(payload.get("worktrees_root"), "worktrees_root")
    ).expanduser().resolve()
    git_hooks_path = Path(
        _required_text(payload.get("git_hooks_path"), "git_hooks_path")
    ).expanduser().resolve()
    prefix = _required_text(payload.get("branch_prefix", "hermes/slack"), "branch_prefix").strip("/")
    if not prefix or any(part in {".", ".."} for part in prefix.split("/")):
        raise RouterError("branch_prefix is invalid")
    return Config(
        state_db=state_db,
        worktrees_root=root,
        git_hooks_path=git_hooks_path,
        branch_prefix=prefix,
        deny_unmapped=bool(payload.get("deny_unmapped", True)),
        terminal_isolation=isolation,
        terminal_image=image,
        terminal_network=network,
        terminal_pass_env=pass_env,
        backend=backend,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_key=ssh_key,
        helper_command=helper_command,
        helper_timeout=helper_timeout,
        codex_binary=codex_binary,
        codex_home=codex_home,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        codex_sandbox=codex_sandbox,
        codex_timeout=codex_timeout,
        routes=routes,
    )


class Router:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def config(self) -> Config:
        # Reload on every hook so operator route changes take effect without a
        # plugin reinstall. The file is tiny compared with a model turn.
        return _load_config()

    @staticmethod
    def _ssh_control_path(config: Config, known_hosts: str) -> Path:
        # Short, container-local path: Unix sockets have a small path limit.
        # Share across gateway/diagnostic processes, never across credentials.
        root = _SSH_RUNTIME_ROOT / f'atlas-ssh-{os.getuid()}'
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise RouterError('Atlas SSH socket directory must be owned by this user with mode 0700')
        key = Path(config.ssh_key) if config.ssh_key else None
        key_info = key.stat() if key and key.exists() else None
        identity = [config.ssh_host, config.ssh_port, config.ssh_user,
                    config.ssh_key, known_hosts, config.helper_command,
                    (key_info.st_ino, key_info.st_mtime_ns, key_info.st_size) if key_info else None]
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        return root / digest

    @staticmethod
    def _ssh_argv(config: Config) -> list[str]:
        known_hosts = os.environ.get(
            "HERMES_SLACK_WORKTREE_KNOWN_HOSTS",
            "/data/.hermes/atlas-known-hosts",
        ).strip()
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60",
            "-o", f"ControlPath={Router._ssh_control_path(config, known_hosts)}",
            "-p", str(config.ssh_port),
        ]
        if config.ssh_key:
            argv.extend(["-i", config.ssh_key])
        argv.extend([f"{config.ssh_user}@{config.ssh_host}", config.helper_command])
        return argv

    @staticmethod
    @contextmanager
    def _ssh_request_lock(argv: list[str], timeout: float):
        # Serialize short helper RPCs, including first connection establishment,
        # so simultaneous processes cannot create a burst of new SSH masters.
        # Coding runs independently in the DO worker, outside this lock.
        control = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('ControlPath='))
        deadline = time.monotonic() + timeout
        with Path(control + '.lock').open('a+') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    time.sleep(0.05)
            try:
                yield max(0.001, deadline - time.monotonic())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _remote_request(
        self,
        config: Config,
        operation: str,
        **payload: Any,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex[:12]
        request = {"version": 1, "request_id": request_id, "operation": operation, **payload}
        log_event(
            logging.INFO,
            "helper_request",
            request_id=request_id,
            operation=operation,
            host=config.ssh_host,
        )
        # A same-thread message may wait behind active Codex turns. Keep the
        # SSH request alive long enough for a small burst to drain in order.
        timeout = config.codex_timeout * 4 + 30 if operation == "codex_run" else config.helper_timeout
        if operation in {'job_submit', 'job_status'}:
            timeout = min(config.helper_timeout, 30)
        try:
            argv = self._ssh_argv(config)
            with self._ssh_request_lock(argv, timeout) as remaining:
                result = subprocess.run(
                    argv,
                    input=json.dumps(request, separators=(",", ":")),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=remaining,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            error = RouterError(
                f"DigitalOcean workspace helper timed out during {operation}",
                error_id=request_id,
            )
            log_event(
                logging.ERROR,
                "helper_timeout",
                request_id=request_id,
                operation=operation,
                timeout=timeout,
            )
            raise error from exc
        except OSError as exc:
            error = RouterError(
                f"could not start the DigitalOcean workspace helper: {exc}",
                error_id=request_id,
            )
            log_event(
                logging.ERROR,
                "helper_start_failed",
                request_id=request_id,
                operation=operation,
                error=str(exc),
            )
            raise error from exc

        stderr_tail = result.stderr.strip()[-2000:]
        if stderr_tail:
            log_event(
                logging.INFO if result.returncode == 0 else logging.ERROR,
                "helper_stderr",
                request_id=request_id,
                operation=operation,
                detail=stderr_tail,
            )
        try:
            response = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            error = RouterError(
                (f"DigitalOcean connection interrupted during {operation} (SSH exit {result.returncode}); "
                 "job outcome is unknown" if result.returncode != 0 and not result.stdout.strip()
                 else f"DigitalOcean workspace helper returned invalid JSON during {operation}"),
                error_id=request_id,
            )
            log_event(
                logging.ERROR,
                ("helper_connection_interrupted" if result.returncode != 0 and not result.stdout.strip()
                 else "helper_invalid_response"),
                request_id=request_id,
                operation=operation,
                returncode=result.returncode,
                stdout_tail=result.stdout.strip()[-1000:],
            )
            raise error from exc
        if not isinstance(response, dict):
            raise RouterError("workspace helper response is not an object", error_id=request_id)
        response_id = str(response.get("request_id") or "")
        if response_id != request_id:
            raise RouterError("workspace helper response ID mismatch", error_id=request_id)
        if result.returncode != 0 or response.get("ok") is not True:
            message = str(response.get("error") or f"workspace helper exited {result.returncode}")
            remote_error_id = str(response.get("error_id") or request_id)
            log_event(
                logging.ERROR,
                "helper_rejected",
                request_id=request_id,
                error_id=remote_error_id,
                operation=operation,
                error=message,
            )
            raise RouterError(message, error_id=remote_error_id)
        log_event(logging.INFO, "helper_success", request_id=request_id, operation=operation)
        return response

    def run_codex(self, mapping: Mapping, prompt: str) -> dict[str, Any]:
        config = self.config()
        if config.backend != "ssh":
            raise RouterError("Codex execution is only supported through the SSH helper")
        response = self._remote_request(
            config,
            "codex_run",
            session_id=mapping.session_id,
            expected=mapping.to_dict(),
            prompt=prompt,
        )
        thread_id = str(response.get("codex_thread_id") or "")
        final = str(response.get("final") or "").strip()
        if not thread_id:
            raise RouterError("Codex helper returned no thread ID")
        if not final:
            raise RouterError("Codex helper returned no final response")
        return {
            "codex_thread_id": thread_id,
            "final": final,
            "resumed": bool(response.get("resumed")),
        }

    @staticmethod
    def _connect(config: Config) -> sqlite3.Connection:
        config.state_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.state_db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_mappings (
                session_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                repo TEXT NOT NULL,
                github_repo TEXT NOT NULL,
                worktree TEXT NOT NULL UNIQUE,
                branch TEXT NOT NULL UNIQUE,
                base_branch TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(workspace_id, channel_id, thread_ts)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_threads (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                session_id TEXT NOT NULL,
                github_repo TEXT NOT NULL,
                branch TEXT NOT NULL,
                reason TEXT NOT NULL,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(workspace_id, channel_id, thread_ts)
            )
            """
        )
        return conn

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row) -> Mapping:
        return Mapping(
            session_id=row["session_id"],
            workspace_id=row["workspace_id"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            repo=Path(row["repo"]),
            github_repo=row["github_repo"],
            worktree=Path(row["worktree"]),
            branch=row["branch"],
            base_branch=row["base_branch"],
            base_sha=row["base_sha"],
        )

    def lookup(self, session_id: str, config: Config | None = None) -> Mapping | None:
        cfg = config or self.config()
        if cfg.backend == "ssh":
            response = self._remote_request(cfg, "lookup", session_id=session_id)
            raw = response.get("mapping")
            mapping = Mapping.from_dict(raw) if isinstance(raw, dict) else None
            if mapping is not None and mapping.session_id != session_id:
                raise RouterError("workspace helper lookup returned the wrong session")
            return mapping
        with self._connect(cfg) as conn:
            row = conn.execute(
                "SELECT * FROM thread_mappings WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row_to_mapping(row) if row else None

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
        if not slug:
            raise RouterError("Slack thread id cannot be converted to a safe slug")
        return slug[:80]

    @staticmethod
    def _assert_repo(route: Route, config: Config) -> None:
        if not route.repo.is_dir():
            raise RouterError(f"repository does not exist: {route.repo}")
        top = Path(_run_git("rev-parse", "--show-toplevel", cwd=route.repo)).resolve()
        if top != route.repo:
            raise RouterError(f"configured repo is not its Git top level: {route.repo}")
        origin = _run_git("remote", "get-url", "origin", cwd=route.repo)
        normalized = origin.removesuffix(".git").replace("git@github.com:", "github.com/")
        normalized = normalized.replace("https://", "").replace("ssh://git@", "")
        if not normalized.endswith(f"github.com/{route.github_repo}"):
            raise RouterError(
                f"repository origin {origin!r} does not match {route.github_repo!r}"
            )
        pre_push = config.git_hooks_path / "pre-push"
        if not pre_push.is_file() or not os.access(pre_push, os.X_OK):
            raise RouterError(f"Atlas pre-push guard is missing or not executable: {pre_push}")
        configured_hooks = Path(
            _run_git("config", "--get", "core.hooksPath", cwd=route.repo)
        ).expanduser()
        if not configured_hooks.is_absolute():
            configured_hooks = route.repo / configured_hooks
        if configured_hooks.resolve() != config.git_hooks_path:
            raise RouterError(
                f"repository core.hooksPath is not the Atlas guard: {route.repo}"
            )

    def provision(
        self,
        *,
        session_id: str,
        workspace_id: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> Mapping:
        config = self.config()
        route = config.routes.get(channel_id)
        if route is None:
            raise RouterError(f"Slack channel {channel_id} has no repository route")
        if route.workspace_id != workspace_id:
            raise RouterError(
                f"Slack channel {channel_id} is not routed for workspace {workspace_id}"
            )
        if user_id not in route.allowed_users:
            raise RouterError(f"Slack user {user_id} is not allowed for channel {channel_id}")
        if config.backend == "ssh":
            response = self._remote_request(
                config,
                "provision",
                session_id=session_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
            )
            raw = response.get("mapping")
            if not isinstance(raw, dict):
                raise RouterError("workspace helper omitted the provisioned mapping")
            mapping = Mapping.from_dict(raw)
            if (
                mapping.session_id != session_id
                or mapping.workspace_id != workspace_id
                or mapping.channel_id != channel_id
                or mapping.thread_ts != thread_ts
            ):
                raise RouterError("workspace helper returned the wrong Slack-thread identity")
            self._validate_mapping_route(mapping, route, config)
            return mapping
        self._assert_repo(route, config)
        repo_name = self._slug(route.github_repo.rsplit("/", 1)[-1])
        thread_slug = self._slug(thread_ts)
        channel_slug = self._slug(channel_id)
        branch = f"{config.branch_prefix}-{channel_slug}-{thread_slug}"
        worktree = (config.worktrees_root / repo_name / thread_slug).resolve()
        try:
            worktree.relative_to(config.worktrees_root)
        except ValueError as exc:
            raise RouterError("resolved worktree escapes worktrees_root") from exc

        lock_dir = config.state_db.parent / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{repo_name}.lock"
        with self._lock, lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            with self._connect(config) as conn:
                archived = conn.execute(
                    "SELECT reason, archived_at FROM archived_threads "
                    "WHERE workspace_id=? AND channel_id=? AND thread_ts=?",
                    (workspace_id, channel_id, thread_ts),
                ).fetchone()
                if archived:
                    raise RouterError(
                        "this Slack thread's coding workspace was archived "
                        f"({archived['reason']} at {archived['archived_at']} UTC); "
                        "start a new top-level Slack thread for more work"
                    )
                row = conn.execute(
                    "SELECT * FROM thread_mappings WHERE workspace_id=? AND channel_id=? AND thread_ts=?",
                    (workspace_id, channel_id, thread_ts),
                ).fetchone()
                if row:
                    mapping = self._row_to_mapping(row)
                    if mapping.session_id != session_id:
                        conn.execute(
                            "UPDATE thread_mappings SET session_id=?, updated_at=CURRENT_TIMESTAMP WHERE workspace_id=? AND channel_id=? AND thread_ts=?",
                            (session_id, workspace_id, channel_id, thread_ts),
                        )
                        mapping = Mapping(session_id=session_id, **{
                            key: getattr(mapping, key)
                            for key in Mapping.__dataclass_fields__
                            if key != "session_id"
                        })
                    else:
                        conn.execute(
                            "UPDATE thread_mappings SET updated_at=CURRENT_TIMESTAMP "
                            "WHERE session_id=?",
                            (session_id,),
                        )
                    conn.commit()
                    self._verify_or_recover(mapping, config)
                    return mapping

                if worktree.exists():
                    raise RouterError(f"unregistered worktree already exists: {worktree}")
                try:
                    _run_git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=route.repo)
                except RouterError:
                    pass
                else:
                    raise RouterError(f"unregistered branch already exists: {branch}")

                _run_git("fetch", "--prune", "origin", route.base_branch, cwd=route.repo)
                base_ref = f"refs/remotes/origin/{route.base_branch}"
                base_sha = _run_git("rev-parse", "--verify", base_ref, cwd=route.repo)
                worktree.parent.mkdir(parents=True, exist_ok=True)
                # Persist intent before the Git mutation. If the helper is
                # killed after `worktree add`, the next identical request has
                # enough information to verify/recover instead of producing an
                # opaque unregistered-directory failure.
                conn.execute(
                    """
                    INSERT INTO thread_mappings (
                        session_id, workspace_id, channel_id, thread_ts,
                        repo, github_repo, worktree, branch, base_branch, base_sha
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id, workspace_id, channel_id, thread_ts,
                        str(route.repo), route.github_repo, str(worktree), branch,
                        route.base_branch, base_sha,
                    ),
                )
                conn.commit()
                try:
                    _run_git(
                        "worktree", "add", str(worktree), "-b", branch, base_sha,
                        cwd=route.repo,
                    )
                except Exception:
                    # Best-effort rollback only for artifacts created by this
                    # operation. Preserve the original exception and let the
                    # DB transaction roll back even if cleanup itself fails.
                    try:
                        if worktree.exists():
                            _run_git("worktree", "remove", "--force", str(worktree), cwd=route.repo)
                    except Exception as cleanup_exc:
                        log_event(
                            logging.ERROR,
                            "provision_rollback_failed",
                            session_id=session_id,
                            worktree=worktree,
                            detail=str(cleanup_exc),
                        )
                    try:
                        _run_git("branch", "-D", branch, cwd=route.repo)
                    except Exception:
                        pass
                    raise
        mapping = self.lookup(session_id, config)
        if mapping is None:
            raise RouterError("mapping insert did not persist")
        self.verify(mapping, config)
        return mapping

    def _verify_or_recover(self, mapping: Mapping, config: Config) -> None:
        try:
            self.verify(mapping, config)
            return
        except RouterError as original:
            if mapping.worktree.exists():
                raise
            try:
                _run_git("rev-parse", "--verify", f"refs/heads/{mapping.branch}", cwd=mapping.repo)
            except RouterError:
                branch_exists = False
            else:
                branch_exists = True
            if branch_exists:
                raise RouterError(
                    "workspace recovery stopped: mapped worktree is missing but its "
                    f"branch still exists ({mapping.branch}); inspect Git worktree state",
                    error_id=original.error_id,
                ) from original
            log_event(
                logging.WARNING,
                "workspace_recovery_started",
                session_id=mapping.session_id,
                worktree=mapping.worktree,
                branch=mapping.branch,
                base_sha=mapping.base_sha,
            )
            mapping.worktree.parent.mkdir(parents=True, exist_ok=True)
            _run_git(
                "worktree", "add", str(mapping.worktree), "-b", mapping.branch,
                mapping.base_sha, cwd=mapping.repo,
            )
            self.verify(mapping, config)
            log_event(
                logging.INFO,
                "workspace_recovery_succeeded",
                session_id=mapping.session_id,
                worktree=mapping.worktree,
                branch=mapping.branch,
            )

    @staticmethod
    def _validate_mapping_route(mapping: Mapping, route: Route, config: Config) -> None:
        if mapping.workspace_id != route.workspace_id or mapping.channel_id != route.channel_id:
            raise RouterError("workspace helper returned the wrong Slack workspace/channel")
        if mapping.repo != route.repo or mapping.github_repo != route.github_repo:
            raise RouterError("workspace helper returned a repository outside the configured route")
        if mapping.base_branch != route.base_branch:
            raise RouterError("workspace helper returned the wrong base branch")
        if not mapping.branch.startswith(f"{config.branch_prefix}-"):
            raise RouterError("workspace helper returned a branch outside the configured prefix")
        try:
            mapping.worktree.relative_to(config.worktrees_root)
        except ValueError as exc:
            raise RouterError("workspace helper returned a path outside worktrees_root") from exc

    def verify(self, mapping: Mapping, config: Config | None = None) -> None:
        cfg = config or self.config()
        if cfg.backend == "ssh":
            response = self._remote_request(
                cfg,
                "verify",
                session_id=mapping.session_id,
                expected=mapping.to_dict(),
            )
            raw = response.get("mapping")
            if not isinstance(raw, dict) or Mapping.from_dict(raw) != mapping:
                raise RouterError("workspace helper verification returned a different mapping")
            return
        worktree = mapping.worktree.resolve()
        repo = mapping.repo.resolve()
        if cfg is not None:
            try:
                worktree.relative_to(cfg.worktrees_root)
            except ValueError as exc:
                raise RouterError("mapped worktree escapes worktrees_root") from exc
        if not worktree.is_dir():
            raise RouterError(f"mapped worktree is missing: {worktree}")
        top = Path(_run_git("rev-parse", "--show-toplevel", cwd=worktree)).resolve()
        if top != worktree:
            raise RouterError(f"mapped path is not the expected worktree: {worktree}")
        try:
            branch = _run_git("symbolic-ref", "--short", "HEAD", cwd=worktree)
        except RouterError as exc:
            raise RouterError(
                f"worktree branch changed: expected {mapping.branch}, found detached HEAD"
            ) from exc
        if branch != mapping.branch:
            raise RouterError(f"worktree branch changed: expected {mapping.branch}, found {branch}")
        common = Path(_run_git("rev-parse", "--git-common-dir", cwd=worktree))
        if not common.is_absolute():
            common = worktree / common
        expected_common = repo / ".git"
        if common.resolve() != expected_common.resolve():
            raise RouterError("worktree belongs to a different repository")

    @staticmethod
    def _inside_local(worktree: Path, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = worktree / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(worktree.resolve())
        except ValueError as exc:
            raise RouterError(f"path escapes the mapped worktree: {raw_path}") from exc
        return resolved

    def _inside(self, mapping: Mapping, raw_path: str, config: Config) -> Path:
        if config.backend == "ssh":
            response = self._remote_request(
                config,
                "resolve_path",
                session_id=mapping.session_id,
                path=raw_path,
            )
            resolved = response.get("path")
            if not isinstance(resolved, str) or not resolved.startswith("/"):
                raise RouterError("workspace helper returned an invalid resolved path")
            result = Path(resolved)
            try:
                result.relative_to(mapping.worktree)
            except ValueError as exc:
                raise RouterError("workspace helper resolved a path outside the worktree") from exc
            return result
        return self._inside_local(mapping.worktree, raw_path)

    def _rewrite_patch(self, patch: str, mapping: Mapping, config: Config) -> str:
        seen = 0

        def single(match: re.Match[str]) -> str:
            nonlocal seen
            seen += 1
            path = self._inside(mapping, match.group(2).strip(), config)
            return f"{match.group(1)}{path}"

        def move(match: re.Match[str]) -> str:
            nonlocal seen
            seen += 1
            source = self._inside(mapping, match.group(2).strip(), config)
            target = self._inside(mapping, match.group(3).strip(), config)
            return f"{match.group(1)}{source} -> {target}"

        rewritten = _PATCH_PATH.sub(single, patch)
        rewritten = _PATCH_MOVE.sub(move, rewritten)
        if seen == 0:
            raise RouterError("patch contains no recognized file headers")
        return rewritten

    @staticmethod
    def _docker_command(command: str, mapping: Mapping, config: Config) -> str:
        repo_git = (mapping.repo / ".git").resolve()
        argv = [
            "docker", "run", "--rm", "--init",
            "--network", config.terminal_network,
            "--mount", f"type=bind,src={mapping.worktree},dst={mapping.worktree}",
            "--mount", f"type=bind,src={repo_git},dst={repo_git}",
            "--workdir", str(mapping.worktree),
        ]
        for name in config.terminal_pass_env:
            argv.extend(["--env", name])
        argv.extend([config.terminal_image, "/bin/sh", "-lc", command])
        return shlex.join(argv)

    def tool_directive(self, tool_name: str, args: Any, session_id: str) -> dict[str, Any] | None:
        if tool_name not in PROTECTED_TOOLS:
            return None
        try:
            config = self.config()
            mapping = self.lookup(session_id, config) if session_id else None
            if mapping is None:
                if config.deny_unmapped:
                    raise RouterError("coding tools require a registered Slack-thread workspace")
                return None
            self.verify(mapping, config)
            if tool_name in UNCONFINED_TOOLS:
                raise RouterError(f"{tool_name} does not inherit the worktree boundary")
            if not isinstance(args, dict):
                raise RouterError(f"{tool_name} arguments are not a mapping")
            modified = dict(args)
            if tool_name == TERMINAL_TOOL:
                command = str(modified.get("command") or "")
                if not command:
                    raise RouterError("terminal command is empty")
                modified["workdir"] = str(mapping.worktree)
                if config.terminal_isolation == "docker":
                    modified["command"] = self._docker_command(command, mapping, config)
            elif tool_name == PATCH_TOOL:
                if modified.get("mode", "replace") == "patch":
                    modified["patch"] = self._rewrite_patch(
                        str(modified.get("patch") or ""), mapping, config
                    )
                else:
                    modified["path"] = str(
                        self._inside(mapping, str(modified.get("path") or ""), config)
                    )
            else:
                raw_path = str(modified.get("path") or ".")
                modified["path"] = str(self._inside(mapping, raw_path, config))
            return {"action": "modify", "args": modified}
        except Exception as exc:
            error_id = getattr(exc, "error_id", uuid.uuid4().hex[:12])
            log_event(
                logging.ERROR,
                "tool_blocked",
                error_id=error_id,
                session_id=session_id,
                tool=tool_name,
                error=str(exc),
            )
            return {
                "action": "block",
                "message": f"{PLUGIN_NAME} [{error_id}]: {safe_detail(exc, 800)}",
            }

    def abandon(self, session_id: str) -> dict[str, Any]:
        config = self.config()
        if config.backend != "ssh":
            raise RouterError("workspace abandon is only supported through the SSH helper")
        return self._remote_request(config, "abandon", session_id=session_id)

    @staticmethod
    def prompt(mapping: Mapping) -> str:
        return (
            "You are in a Slack-thread coding workspace enforced by the "
            f"{PLUGIN_NAME} plugin.\n"
            f"Repository: {mapping.github_repo}\n"
            f"Branch: {mapping.branch}\n"
            f"Base: {mapping.base_branch} @ {mapping.base_sha}\n"
            f"Workspace: {mapping.worktree} (isolated)\n\n"
            "GitHub is the source of truth. Keep the request and decisions in a "
            "GitHub issue or PR, commit only to the mapped branch, run the "
            "repository's verification, push using the mapped branch name as "
            "both the local and remote ref (never HEAD), and open a PR. Never "
            "merge or change production configuration. Hermes reports the "
            "workspace, branch, base, model, and reasoning effort separately; "
            "do not repeat those details in your response."
        )


def normalize_platform(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def authorized_route(
    config: Config,
    workspace_id: str,
    channel_id: str,
    user_id: str,
) -> Route | None:
    route = config.routes.get(channel_id)
    if (
        route is None
        or route.workspace_id != workspace_id
        or user_id not in route.allowed_users
    ):
        return None
    global_allowed = {
        item.strip() for item in os.environ.get("SLACK_ALLOWED_USERS", "").split(",") if item.strip()
    }
    if global_allowed and user_id not in global_allowed:
        return None
    return route
