"""Cap-only compatibility with the user-approved Railway container boundary."""
import os
from pathlib import Path


def container_boundary(env=None, config_path=None):
    env = os.environ if env is None else env
    if env.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap':
        return False
    import tomllib
    path = Path(config_path or '/data/.codex/config.toml')
    try:
        config = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return config.get('sandbox_mode') == 'danger-full-access'


def configure_spawn(child, parent=None):
    """Allow Cap's intended GitHub credential, not other Hermes secrets."""
    parent = os.environ if parent is None else parent
    if parent.get('RAILWAY_SERVICE_NAME') == 'Hermes-Cap' and parent.get('GH_TOKEN'):
        child['GH_TOKEN'] = parent['GH_TOKEN']


def patch_source(source):
    anchor = '        spawn_env = hermes_subprocess_env(inherit_credentials=True)'
    replacement = (anchor + '\n'
                   '        from agent.cap_runtime_policy import configure_spawn, container_boundary\n'
                   '        configure_spawn(spawn_env)')
    condition = '        if spawn_env.get("HERMES_KANBAN_TASK"):'
    new_condition = ('        if spawn_env.get("HERMES_KANBAN_TASK") '
                     'and not container_boundary():')
    if source.count(anchor) != 1 or source.count(condition) != 1:
        raise RuntimeError('Hermes Codex adapter changed; review Cap compatibility patch')
    return source.replace(anchor, replacement).replace(condition, new_condition)


if __name__ == '__main__':
    target = Path('/opt/hermes-agent/agent/transports/codex_app_server.py')
    updated = patch_source(target.read_text())
    compile(updated, str(target), 'exec')
    target.write_text(updated)
