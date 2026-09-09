import importlib.util
from pathlib import Path

import pytest
import yaml

CAP = Path(__file__).parents[1] / 'cap'
spec = importlib.util.spec_from_file_location('cap_bootstrap', CAP / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def test_fresh_cap_is_native_but_does_not_dispatch(tmp_path):
    result = bootstrap.seed(tmp_path, CAP, service='Hermes-Cap')
    config = yaml.safe_load((tmp_path / '.hermes/config.yaml').read_text())
    assert result['created'] == ['config.yaml', 'SOUL.md', '.codex/config.toml']
    assert config['model']['openai_runtime'] == 'codex_app_server'
    assert config['plugins']['enabled'] == []
    assert config['kanban']['dispatch_in_gateway'] is False
    assert (tmp_path / '.codex').stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / '.codex/auth.json').exists()
    assert 'explicit approval' in (tmp_path / '.hermes/SOUL.md').read_text()
    codex_config = tmp_path / '.codex/config.toml'
    policy = [line for line in codex_config.read_text().splitlines()
              if line and not line.startswith('#')]
    assert policy == ['sandbox_mode = "danger-full-access"',
                      'approval_policy = "on-request"']
    assert codex_config.stat().st_mode & 0o777 == 0o600


def test_redeploy_preserves_configuration_and_identity(tmp_path):
    bootstrap.seed(tmp_path, CAP, service='Hermes-Cap')
    config = tmp_path / '.hermes/config.yaml'
    soul = tmp_path / '.hermes/SOUL.md'
    config.write_text('operator_configuration: keep\n')
    soul.write_text('Operator-customized Cap')
    codex_config = tmp_path / '.codex/config.toml'
    codex_config.write_text('sandbox_mode = "read-only"\n')
    auth = tmp_path / '.codex/auth.json'
    auth.write_text('{"test_credential": "preserve"}\n')
    assert bootstrap.seed(tmp_path, CAP, service='Hermes-Cap')['created'] == []
    assert config.read_text() == 'operator_configuration: keep\n'
    assert soul.read_text() == 'Operator-customized Cap'
    assert codex_config.read_text() == 'sandbox_mode = "read-only"\n'
    assert auth.read_text() == '{"test_credential": "preserve"}\n'


@pytest.mark.parametrize('service', ['Hermes-Team', 'Hermes-Owners', ''])
def test_cap_image_refuses_other_services(tmp_path, service):
    with pytest.raises(RuntimeError, match='Hermes-Cap'):
        bootstrap.seed(tmp_path, CAP, service=service)
    assert not (tmp_path / '.hermes').exists()
    assert not (tmp_path / '.codex').exists()
