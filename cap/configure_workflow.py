"""One-time native workflow setup; never modifies Railway env vars or tokens."""
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

REPOS = ('vw-site', 'vw-hq', 'vw-crm', 'vw-dashboards')


def configure(config):
    config = dict(config)
    toolsets = list(config.get('toolsets') or [])
    if 'kanban' not in toolsets:
        toolsets.append('kanban')
    config['toolsets'] = toolsets
    config['terminal'] = {**config.get('terminal', {}), 'cwd': '/data/cap/workspace'}
    config['kanban'] = {**config.get('kanban', {}),
                        'dispatch_in_gateway': True, 'review_dispatch': False,
                        'auto_decompose': False, 'dispatch_interval_seconds': 10,
                        'max_in_progress': 2, 'max_in_progress_per_profile': 2,
                        'default_assignee': 'default', 'orchestrator_profile': 'default'}
    return config


def add_callback_context(source):
    # Codex's MCP child environment is explicitly allowlisted. Preserve native
    # task/run ownership and conversation routing, not any Slack/admin secrets.
    names = ['HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK',
             'HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_WORKSPACE',
             'HERMES_KANBAN_WORKSPACES_ROOT', 'HERMES_PROFILE',
             'HERMES_SESSION_PLATFORM', 'HERMES_SESSION_CHAT_ID',
             'HERMES_SESSION_CHAT_TYPE', 'HERMES_SESSION_THREAD_ID',
             'HERMES_SESSION_USER_ID', 'HERMES_SESSION_USER_ID_ALT',
             'HERMES_SESSION_USER_NAME', 'HERMES_SESSION_SCOPE_ID',
             'HERMES_SESSION_KEY', 'HERMES_SESSION_ID', 'HERMES_SESSION_SOURCE']
    header = '[mcp_servers.hermes-tools]'
    if source.count(header) != 1:
        raise RuntimeError('Native MCP registration format changed')
    return source.replace(header, header + '\nenv_vars = ' + json.dumps(names))


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode:
        # Neither credentials nor untrusted command output belong in boot logs.
        raise RuntimeError(f'Cap setup command {args[0]} failed ({result.returncode})')
    return result.stdout.strip()


def ensure_repo_exclusions(root):
    # Native Hermes creates task worktrees below the primary checkout. Keep
    # those host-local directories out of git status without editing .gitignore.
    for repo in REPOS:
        git_dir = root / 'repos' / repo / '.git'
        if not git_dir.is_dir():
            continue
        target = git_dir / 'info' / 'exclude'
        target.parent.mkdir(exist_ok=True)
        previous = target.read_text() if target.exists() else ''
        if '/.worktrees/' not in previous.splitlines():
            with target.open('a') as f:
                f.write(('\n' if previous and not previous.endswith('\n') else '')
                        + '/.worktrees/\n')


def main():
    if os.environ.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap':
        raise RuntimeError('Workflow setup is restricted to Hermes-Cap')
    root = Path('/data/cap')
    ensure_repo_exclusions(root)
    guide = Path('/app/cap/routing-guide.md')
    soul = Path('/data/.hermes/SOUL.md')
    if guide.exists() and soul.exists() and "## Cap's enforced Slack routing (v1)" not in soul.read_text():
        with soul.open('a') as stream:
            stream.write('\n' + guide.read_text())
    marker = root / '.native-workflow-v1'
    if marker.exists():
        print(json.dumps({'event': 'cap_workflow_setup', 'status': 'already_configured'}))
        return
    if not os.environ.get('GH_TOKEN'):
        raise RuntimeError('Cap workflow setup requires the operator-provided GH_TOKEN')
    sys.path.insert(0, '/opt/hermes-agent')
    from hermes_cli import kanban_db as kb
    from hermes_cli.codex_runtime_plugin_migration import migrate

    for repo in REPOS:
        target = root / 'repos' / repo
        url = f'https://github.com/gldnlab/{repo}.git'
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            run('git', '-c', 'credential.helper=!gh auth git-credential',
                'clone', url, str(target))
        if run('git', '-C', str(target), 'remote', 'get-url', 'origin') != url:
            raise RuntimeError(f'Unexpected existing origin for {repo}; refusing to replace it')
        run('git', '-C', str(target), 'config', 'credential.https://github.com.helper',
            '!gh auth git-credential')
        run('git', '-C', str(target), 'config', 'user.name', 'Cap')
        run('git', '-C', str(target), 'config', 'user.email', 'cap@users.noreply.github.com')
        if not kb.board_exists(repo):
            kb.create_board(repo, name=repo, description=f'gldnlab/{repo} — Cap coding tasks',
                            default_workdir=str(target))
        print(json.dumps({'event': 'cap_repository_ready', 'repo': repo}))

    ensure_repo_exclusions(root)

    config_path = Path('/data/.hermes/config.yaml')
    original = config_path.read_text()
    config = configure(yaml.safe_load(original) or {})
    # Backup is config only; do not read/copy auth.json or .env.
    backup = root / 'config.before-native-workflow.yaml'
    if not backup.exists():
        backup.write_text(original)
        backup.chmod(0o600)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    report = migrate(config, codex_home=Path('/data/.codex'), discover_plugins=False,
                     default_permission_profile=None, expose_hermes_tools=True)
    if report.errors or not report.written:
        raise RuntimeError('Native Hermes-to-Codex MCP registration failed')
    codex_config = Path('/data/.codex/config.toml')
    updated = add_callback_context(codex_config.read_text())
    import tomllib
    parsed = tomllib.loads(updated)
    if parsed.get('sandbox_mode') != 'danger-full-access':
        raise RuntimeError('Native migration changed Cap execution policy')
    codex_config.write_text(updated)
    soul = Path('/data/.hermes/SOUL.md')
    instructions = Path('/app/cap/workflow.md').read_text()
    if "## Cap's connected coding workflow" not in soul.read_text():
        with soul.open('a') as f:
            f.write('\n' + instructions)
    marker.write_text('Native Kanban + Codex MCP + repositories configured\n')
    print(json.dumps({'event': 'cap_workflow_setup', 'status': 'configured',
                      'automatic_review_dispatch': False, 'max_workers': 2}))


if __name__ == '__main__':
    main()
