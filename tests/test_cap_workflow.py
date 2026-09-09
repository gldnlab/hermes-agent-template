import importlib.util
from pathlib import Path

import pytest

CAP = Path(__file__).parents[1] / 'cap'


def load(name):
    spec = importlib.util.spec_from_file_location(name, CAP / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workflow = load('configure_workflow')
policy = load('runtime_policy')


def test_configure_preserves_user_settings_and_requires_human_review():
    original = {'display': {'show_commentary': False}, 'toolsets': ['web'],
                'kanban': {'custom': 'keep'}, 'model': {'default': 'gpt-6-astra'}}
    result = workflow.configure(original)
    assert result['display'] == original['display']
    assert result['model'] == original['model']
    assert result['toolsets'] == ['web', 'kanban']
    assert result['kanban']['custom'] == 'keep'
    assert result['kanban']['dispatch_in_gateway'] is True
    assert result['kanban']['review_dispatch'] is False
    assert result['kanban']['auto_decompose'] is False
    assert result['kanban']['max_in_progress'] == 2
    assert original['toolsets'] == ['web']
    assert workflow.configure(result) == result


def test_only_caps_github_credential_is_forwarded():
    child = {'PATH': '/bin'}
    parent = {'RAILWAY_SERVICE_NAME': 'Hermes-Cap', 'GH_TOKEN': 'test-only',
              'SLACK_BOT_TOKEN': 'do-not-forward', 'ADMIN_PASSWORD': 'do-not-forward'}
    policy.configure_spawn(child, parent)
    assert child == {'PATH': '/bin', 'GH_TOKEN': 'test-only'}
    for service in ['Hermes-Team', 'Hermes-Owners', '']:
        other = {}
        policy.configure_spawn(other, {**parent, 'RAILWAY_SERVICE_NAME': service})
        assert other == {}
        assert policy.container_boundary({'RAILWAY_SERVICE_NAME': service}) is False


def test_patch_is_small_and_fails_on_upstream_drift():
    source = ('class Client:\n    def __init__(self):\n'
              '        spawn_env = hermes_subprocess_env(inherit_credentials=True)\n'
              '        if spawn_env.get("HERMES_KANBAN_TASK"):\n            pass\n')
    changed = policy.patch_source(source)
    compile(changed, '<patched>', 'exec')
    assert 'and not container_boundary()' in changed
    assert 'configure_spawn(spawn_env)' in changed
    with pytest.raises(RuntimeError, match='changed'):
        policy.patch_source(changed)


def test_callback_carries_task_identity_but_no_secrets():
    result = workflow.add_callback_context('[mcp_servers.hermes-tools]\ncommand="python"\n')
    assert 'HERMES_KANBAN_CLAIM_LOCK' in result
    assert 'HERMES_SESSION_THREAD_ID' in result
    assert 'GH_TOKEN' not in result
    assert 'SLACK_BOT_TOKEN' not in result
    assert 'ADMIN_PASSWORD' not in result
    with pytest.raises(RuntimeError):
        workflow.add_callback_context('unexpected upstream format')


def test_worktree_exclusion_preserves_existing_entries_and_is_idempotent(tmp_path):
    info = tmp_path / 'repos/vw-site/.git/info'
    info.mkdir(parents=True)
    target = info / 'exclude'
    target.write_text('# operator entries\nprivate-notes')
    workflow.ensure_repo_exclusions(tmp_path)
    once = target.read_text()
    assert once == '# operator entries\nprivate-notes\n/.worktrees/\n'
    workflow.ensure_repo_exclusions(tmp_path)
    assert target.read_text() == once
