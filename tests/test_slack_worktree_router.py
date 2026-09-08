from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


PLUGIN_DIR = Path(__file__).parents[1] / "plugins" / "slack-worktree-router"


@pytest.fixture(scope="module")
def router_module():
    spec = importlib.util.spec_from_file_location("slack_worktree_router_router", PLUGIN_DIR / "router.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


@pytest.fixture
def configured_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, router_module):
    remote = tmp_path / "github.com" / "velocitywork" / "example.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, stdout=subprocess.PIPE)
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, stdout=subprocess.PIPE)
    hooks = tmp_path / "git-hooks"
    hooks.mkdir()
    pre_push = hooks / "pre-push"
    pre_push.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pre_push.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))

    config_path = tmp_path / "routes.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "deny_unmapped": True,
        "state_db": str(tmp_path / "state" / "routes.sqlite3"),
        "worktrees_root": str(tmp_path / "worktrees"),
        "git_hooks_path": str(hooks),
        "branch_prefix": "hermes/slack",
        "codex": {
            "binary": sys.executable,
            "home": str(tmp_path / "codex-home"),
            "model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "sandbox": "workspace-write",
            "timeout": 300,
        },
        "terminal": {
            "isolation": "docker",
            "image": "example/coder:test",
            "network": "bridge",
            "pass_env": ["GH_TOKEN"],
        },
        "slack_repo_routes": {
            "C123": {
                "workspace_id": "T123",
                "repo": str(repo),
                "github_repo": "velocitywork/example",
                "base_branch": "main",
                "allowed_users": ["U123"],
            }
        },
    }), encoding="utf-8")
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv(router_module.ROUTES_ENV, str(config_path))
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U123")
    return router_module.Router(), repo


def provision(router, **overrides):
    values = {
        "session_id": "session-1",
        "workspace_id": "T123",
        "channel_id": "C123",
        "thread_ts": "1788192345.1204",
        "user_id": "U123",
    }
    values.update(overrides)
    return router.provision(**values)


def test_provision_is_idempotent_and_records_base(configured_router):
    router, repo = configured_router
    mapping = provision(router)
    again = provision(router)
    assert again == mapping
    assert mapping.worktree.is_dir()
    assert mapping.branch == "hermes/slack-c123-1788192345-1204"
    assert mapping.base_sha == git(repo, "rev-parse", "origin/main")
    assert git(mapping.worktree, "branch", "--show-current") == mapping.branch


def test_file_paths_are_rewritten_inside_worktree(configured_router):
    router, _ = configured_router
    mapping = provision(router)
    directive = router.tool_directive("write_file", {"path": "src/app.py", "content": "ok"}, mapping.session_id)
    assert directive["action"] == "modify"
    assert directive["args"]["path"] == str(mapping.worktree / "src/app.py")


def test_file_escape_is_blocked(configured_router):
    router, _ = configured_router
    mapping = provision(router)
    directive = router.tool_directive("read_file", {"path": "../../etc/passwd"}, mapping.session_id)
    assert directive["action"] == "block"
    assert "escapes" in directive["message"]


def test_patch_paths_are_rewritten_and_escape_is_blocked(configured_router):
    router, _ = configured_router
    mapping = provision(router)
    patch = "*** Begin Patch\n*** Update File: README.md\n-old\n+new\n*** End Patch"
    directive = router.tool_directive("patch", {"mode": "patch", "patch": patch}, mapping.session_id)
    assert directive["action"] == "modify"
    assert f"*** Update File: {mapping.worktree / 'README.md'}" in directive["args"]["patch"]
    bad = patch.replace("README.md", "../../outside")
    assert router.tool_directive("patch", {"mode": "patch", "patch": bad}, mapping.session_id)["action"] == "block"


def test_terminal_is_containerized(configured_router):
    router, _ = configured_router
    mapping = provision(router)
    directive = router.tool_directive("terminal", {"command": "git status --short"}, mapping.session_id)
    assert directive["action"] == "modify"
    command = directive["args"]["command"]
    assert command.startswith("docker run --rm --init")
    assert "example/coder:test" in command
    assert directive["args"]["workdir"] == str(mapping.worktree)


def test_unmapped_and_unconfined_tools_fail_closed(configured_router):
    router, _ = configured_router
    assert router.tool_directive("terminal", {"command": "pwd"}, "missing")["action"] == "block"
    mapping = provision(router)
    assert router.tool_directive("execute_code", {"code": "print(1)"}, mapping.session_id)["action"] == "block"
    assert router.tool_directive("delegate_task", {}, mapping.session_id)["action"] == "block"


def test_branch_tampering_fails_closed(configured_router):
    router, _ = configured_router
    mapping = provision(router)
    git(mapping.worktree, "checkout", "--detach")
    directive = router.tool_directive("read_file", {"path": "README.md"}, mapping.session_id)
    assert directive["action"] == "block"
    assert "branch" in directive["message"]


def test_persisted_intent_recovers_missing_worktree(configured_router):
    router, repo = configured_router
    mapping = provision(router)
    git(repo, "worktree", "remove", "--force", str(mapping.worktree))
    git(repo, "branch", "-D", mapping.branch)

    recovered = provision(router)
    assert recovered == mapping
    assert recovered.worktree.is_dir()
    assert git(recovered.worktree, "rev-parse", "HEAD") == mapping.base_sha
    assert git(recovered.worktree, "branch", "--show-current") == mapping.branch


def test_recovery_stops_on_branch_without_worktree(configured_router, router_module):
    router, repo = configured_router
    mapping = provision(router)
    git(repo, "worktree", "remove", "--force", str(mapping.worktree))

    with pytest.raises(router_module.RouterError, match="branch still exists"):
        provision(router)


def test_authorized_route_requires_route_and_global_allowlist(configured_router, router_module, monkeypatch):
    router, _ = configured_router
    config = router.config()
    assert router_module.authorized_route(config, "T123", "C123", "U123") is not None
    assert router_module.authorized_route(config, "T999", "C123", "U123") is None
    assert router_module.authorized_route(config, "T123", "C123", "U999") is None
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U999")
    assert router_module.authorized_route(config, "T123", "C123", "U123") is None


def test_helper_layer_rejects_wrong_workspace_or_user(configured_router, router_module):
    router, _ = configured_router
    with pytest.raises(router_module.RouterError, match="not routed"):
        provision(router, workspace_id="T999")
    with pytest.raises(router_module.RouterError, match="not allowed"):
        provision(router, user_id="U999")


def test_plugin_registers_expected_hermes_surfaces():
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class Context:
        def __init__(self):
            self.hooks = {}
            self.sections = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

        def register_system_prompt_section(self, name, callback, **options):
            self.sections[name] = (callback, options)

    context = Context()
    module.register(context)
    assert set(context.hooks) == {"pre_gateway_dispatch", "pre_tool_call"}
    assert set(context.sections) == {"slack-worktree-router.workspace"}
    _, options = context.sections["slack-worktree-router.workspace"]
    assert options == {"position": "after_memory", "max_chars": 2400}


def test_tool_boundary_ignores_normal_slack_dm_sessions():
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_scope",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeRouter:
        def tool_directive(self, *_args, **_kwargs):
            raise AssertionError("normal DMs must not enter the coding boundary")

    module.ROUTER = FakeRouter()
    assert module._pre_tool_call("terminal", {"command": "pwd"}, "dm-session") is None


def test_tool_boundary_stays_fail_closed_for_routed_sessions():
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_scope_routed",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeRouter:
        def tool_directive(self, tool_name, args, session_id):
            assert (tool_name, session_id) == ("terminal", "routed-session")
            return {"action": "block", "message": "isolated"}

    module.ROUTER = FakeRouter()
    module._ROUTED_SESSIONS.add("routed-session")
    assert module._pre_tool_call(
        "terminal", {"command": "pwd"}, "routed-session"
    ) == {"action": "block", "message": "isolated"}


def test_abandon_command_is_explicit():
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_abandon_command",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module._is_abandon_command("<@UATLAS> abandon this thread") is True
    assert module._is_abandon_command("abandon!") is True
    assert module._is_abandon_command("can you abandon this thread?") is False


def test_remote_helper_protocol_is_json_and_idempotent(configured_router, router_module):
    _, _repo = configured_router
    config_path = os.environ[router_module.ROUTES_ENV]
    helper = PLUGIN_DIR / "remote_helper.py"
    env = dict(os.environ)
    env[router_module.FORCE_LOCAL_ENV] = "1"
    request = {
        "version": 1,
        "request_id": "request-123",
        "operation": "provision",
        "session_id": "remote-session",
        "workspace_id": "T123",
        "channel_id": "C123",
        "thread_ts": "1788192345.9999",
        "user_id": "U123",
    }
    first = subprocess.run(
        [sys.executable, str(helper), "--config", config_path],
        input=json.dumps(request),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["ok"] is True
    assert payload["request_id"] == "request-123"
    assert payload["mapping"]["session_id"] == "remote-session"
    assert '"event": "request_succeeded"' in first.stderr

    second = subprocess.run(
        [sys.executable, str(helper), "--config", config_path],
        input=json.dumps(request),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    assert second.returncode == 0
    assert json.loads(second.stdout)["mapping"] == payload["mapping"]


def test_remote_helper_failure_has_searchable_error_id(configured_router, router_module):
    config_path = os.environ[router_module.ROUTES_ENV]
    result = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "remote_helper.py"), "--config", config_path],
        input=json.dumps({
            "version": 1,
            "request_id": "bad-request",
            "operation": "verify",
            "session_id": "does-not-exist",
        }),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["ok"] is False
    assert payload["request_id"] == "bad-request"
    assert payload["error_id"]
    assert payload["error_id"] in result.stderr


def test_remote_client_preserves_helper_error_id(configured_router, router_module, monkeypatch):
    router, _ = configured_router
    config = replace(
        router.config(),
        backend="ssh",
        ssh_host="coding.example",
        ssh_user="atlas-control",
        ssh_key="/run/secrets/control-key",
    )

    def rejected(*_args, **kwargs):
        request_id = json.loads(kwargs["input"])["request_id"]
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "request_id": request_id,
                "error_id": "remote-error-42",
                "error": "branch mismatch",
            }),
            stderr='{"event":"request_failed","error_id":"remote-error-42"}',
        )

    monkeypatch.setattr(router_module.subprocess, "run", rejected)
    with pytest.raises(router_module.RouterError) as caught:
        router._remote_request(config, "verify", session_id="session-1")
    assert caught.value.error_id == "remote-error-42"
    assert "branch mismatch" in str(caught.value)


def test_error_details_redact_credentials(router_module):
    detail = router_module.safe_detail(
        "fetch https://alice:secret@github.com/org/repo "
        "Authorization: Bearer github_pat_1234567890abcdef"
    )
    assert "secret" not in detail
    assert "github_pat_" not in detail
    assert "alice" not in detail
    assert "[REDACTED]" in detail


def test_direct_slack_error_notification_is_threaded_and_logged(caplog):
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_notify",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class Result:
        success = True
        error = None

    class Adapter:
        name = "slack"

        def __init__(self):
            self.sent = []

        async def send(self, channel, content, reply_to=None, metadata=None):
            self.sent.append((channel, content, reply_to, metadata))
            return Result()

    adapter = Adapter()
    gateway = SimpleNamespace(adapters={"slack": adapter})

    async def scenario():
        scheduled = module._schedule_error(
            gateway,
            channel_id="C123",
            thread_ts="1788192345.1204",
            workspace_id="T123",
            error_id="err-123",
            detail="helper unavailable",
        )
        assert scheduled is True
        await asyncio.sleep(0)

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert adapter.sent[0][0] == "C123"
    assert adapter.sent[0][2] == "1788192345.1204"
    assert adapter.sent[0][3]["slack_team_id"] == "T123"
    assert "err-123" in adapter.sent[0][1]
    assert "atlas_error_delivered" in caplog.text


def test_codex_jsonl_parser_extracts_thread_and_final(router_module):
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_helper",
        PLUGIN_DIR / "remote_helper.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    thread_id, final = module._parse_codex_jsonl("\n".join([
        json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Done and tested."},
        }),
        json.dumps({"type": "turn.completed"}),
    ]))
    assert thread_id == "codex-thread-1"
    assert final == "Done and tested."


def test_codex_jsonl_parser_surfaces_turn_failure(router_module):
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_helper_failure",
        PLUGIN_DIR / "remote_helper.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    with pytest.raises(module.RouterError, match="refresh token"):
        module._parse_codex_jsonl("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"}),
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "Your refresh token is expired"},
            }),
        ]))


def test_codex_preflight_requires_real_workspace_write(
    configured_router, router_module, monkeypatch
):
    router, repo = configured_router
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_helper_preflight",
        PLUGIN_DIR / "remote_helper.py",
    )
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    assert spec.loader is not None
    spec.loader.exec_module(helper)
    real_run = subprocess.run

    def completed(argv, **kwargs):
        if argv[0] != sys.executable:
            return real_run(argv, **kwargs)
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        assert "sandbox_workspace_write.network_access=true" in argv
        match = re.search(
            r"relative file (\S+) containing exactly this text and no newline: (\S+)\.",
            kwargs["input"],
        )
        assert match is not None
        (repo / match.group(1)).write_text(match.group(2), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join([
                json.dumps({"type": "thread.started", "thread_id": "preflight-thread"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "ATLAS_CODEX_WRITE_OK"},
                }),
            ]),
            stderr="",
        )

    monkeypatch.setattr(helper.subprocess, "run", completed)
    result = helper._codex_preflight(router)
    assert result["workspace_write"] is True
    assert not list(repo.glob(".atlas-codex-write-preflight-*"))
    assert git(repo, "status", "--porcelain") == ""


def test_mapped_slack_message_is_dispatched_to_codex_without_hermes_fallback(configured_router):
    router, _ = configured_router
    mapping = provision(router)
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_dispatch",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeRouter:
        def config(self):
            return router.config()

        def provision(self, **_kwargs):
            return mapping

        def prompt(self, _mapping):
            return "workspace prompt"

        def run_codex(self, _mapping, prompt):
            assert "make the change" in prompt
            return {"codex_thread_id": "codex-1", "final": "Done.", "resumed": False}

    class Result:
        success = True
        error = None

    class Adapter:
        name = "slack"

        def __init__(self):
            self.sent = []

        async def send(self, channel, content, reply_to=None, metadata=None):
            self.sent.append((channel, content, reply_to, metadata))
            return Result()

    class SessionStore:
        def get_or_create_session(self, source, touch_activity=False):
            assert touch_activity is False
            return SimpleNamespace(session_id=mapping.session_id)

    adapter = Adapter()
    module.ROUTER = FakeRouter()
    source = SimpleNamespace(
        platform="slack", chat_id="C123", user_id="U123", scope_id="T123", thread_id=None,
    )
    event = SimpleNamespace(source=source, message_id="1788192345.1204", text="make the change")

    async def scenario():
        directive = module._pre_gateway_dispatch(
            event=event,
            gateway=SimpleNamespace(adapters={"slack": adapter}),
            session_store=SessionStore(),
        )
        assert directive == {"action": "skip", "reason": "atlas-codex-dispatched"}
        await asyncio.gather(*list(module._BACKGROUND_TASKS))

    asyncio.run(scenario())
    assert len(adapter.sent) == 1
    assert "Model: `gpt-6-astra` · reasoning: `medium`" in adapter.sent[0][1]
    assert adapter.sent[0][1].endswith("Done.")
    assert all(item[2] == "1788192345.1204" for item in adapter.sent)


def test_unauthorized_user_in_mapped_channel_is_dropped(configured_router):
    router, _ = configured_router
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_unauthorized",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.ROUTER = router
    source = SimpleNamespace(
        platform="slack", chat_id="C123", user_id="U999", scope_id="T123", thread_id=None,
    )
    directive = module._pre_gateway_dispatch(
        event=SimpleNamespace(source=source, message_id="1.2", text="hello"),
        gateway=SimpleNamespace(adapters={}),
        session_store=SimpleNamespace(),
    )
    assert directive == {"action": "skip", "reason": "atlas-route-unauthorized"}


def test_codex_runner_persists_and_resumes_thread(
    configured_router, router_module, monkeypatch
):
    router, _ = configured_router
    mapping = provision(router)
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_helper_resume",
        PLUGIN_DIR / "remote_helper.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    commands = []
    real_run = subprocess.run

    def completed(argv, **kwargs):
        if argv[0] != sys.executable:
            return real_run(argv, **kwargs)
        commands.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join([
                json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Finished."},
                }),
            ]),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", completed)
    first = module._run_codex(router, mapping, "first request")
    second = module._run_codex(router, mapping, "follow-up")

    assert first == {
        "codex_thread_id": "codex-thread-1",
        "final": "Finished.",
        "resumed": False,
    }
    assert second["resumed"] is True
    assert "resume" not in commands[0]
    assert "resume" in commands[1]
    assert "codex-thread-1" in commands[1]
    for command in commands:
        assert command[command.index("--model") + 1] == "gpt-6-astra"
        assert 'model_reasoning_effort="medium"' in command


def test_pre_push_guard_allows_only_atlas_branches():
    hook = PLUGIN_DIR / "deploy" / "atlas-pre-push"
    allowed = subprocess.run(
        [str(hook), "origin", "git@github.com:gldnlab/example.git"],
        input=(
            "refs/heads/atlas/slack-c123 abc123 "
            "refs/heads/atlas/slack-c123 000000\n"
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    blocked = subprocess.run(
        [str(hook), "origin", "git@github.com:gldnlab/example.git"],
        input="refs/heads/main abc123 refs/heads/main def456\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert allowed.returncode == 0
    assert blocked.returncode != 0
    assert "only atlas/* branches" in blocked.stderr


def test_cleanup_expires_only_untouched_inactive_workspace(
    configured_router, router_module, monkeypatch
):
    router, _ = configured_router
    mapping = provision(router)
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_cleanup",
        PLUGIN_DIR / "remote_helper.py",
    )
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    assert spec.loader is not None
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "_remote_branch_sha", lambda _mapping: None)

    with router._connect(router.config()) as conn:
        conn.execute(
            "UPDATE thread_mappings SET updated_at=datetime('now', '-25 hours') "
            "WHERE session_id=?",
            (mapping.session_id,),
        )

    summary = helper.cleanup_stale(router, ttl_hours=24)
    assert [item["reason"] for item in summary["cleaned"]] == ["expired_untouched"]
    assert summary["errors"] == []
    assert not mapping.worktree.exists()
    assert router.lookup(mapping.session_id) is None
    with pytest.raises(router_module.RouterError, match="workspace was archived"):
        provision(router)


def test_cleanup_retains_uncommitted_workspace(
    configured_router, router_module, monkeypatch
):
    router, _ = configured_router
    mapping = provision(router)
    (mapping.worktree / "notes.txt").write_text("keep me\n", encoding="utf-8")
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_cleanup_dirty",
        PLUGIN_DIR / "remote_helper.py",
    )
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    assert spec.loader is not None
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "_remote_branch_sha", lambda _mapping: None)

    with router._connect(router.config()) as conn:
        conn.execute(
            "UPDATE thread_mappings SET updated_at=datetime('now', '-25 hours') "
            "WHERE session_id=?",
            (mapping.session_id,),
        )

    summary = helper.cleanup_stale(router, ttl_hours=24)
    assert summary["cleaned"] == []
    assert summary["retained"] == [
        {"session_id": mapping.session_id, "reason": "uncommitted_changes"}
    ]
    assert mapping.worktree.exists()


def test_explicit_abandon_removes_only_untouched_workspace(
    configured_router, router_module, monkeypatch
):
    router, _ = configured_router
    mapping = provision(router)
    sys.modules["router"] = router_module
    spec = importlib.util.spec_from_file_location(
        "slack_worktree_router_abandon",
        PLUGIN_DIR / "remote_helper.py",
    )
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    assert spec.loader is not None
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "_remote_branch_sha", lambda _mapping: None)

    result = helper.abandon_workspace(router, mapping.session_id)
    assert result["reason"] == "abandoned_untouched"
    assert not mapping.worktree.exists()
    with pytest.raises(router_module.RouterError, match="workspace was archived"):
        provision(router)
