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
    assert result['created'] == ['config.yaml', 'SOUL.md']
    assert config['model']['openai_runtime'] == 'codex_app_server'
    assert config['plugins']['enabled'] == []
    assert config['kanban']['dispatch_in_gateway'] is False
    assert (tmp_path / '.codex').stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / '.codex/auth.json').exists()
    assert 'explicit approval' in (tmp_path / '.hermes/SOUL.md').read_text()


def test_redeploy_preserves_configuration_and_identity(tmp_path):
    bootstrap.seed(tmp_path, CAP, service='Hermes-Cap')
    config = tmp_path / '.hermes/config.yaml'
    soul = tmp_path / '.hermes/SOUL.md'
    config.write_text('operator_configuration: keep\n')
    soul.write_text('Operator-customized Cap')
    assert bootstrap.seed(tmp_path, CAP, service='Hermes-Cap')['created'] == []
    assert config.read_text() == 'operator_configuration: keep\n'
    assert soul.read_text() == 'Operator-customized Cap'


@pytest.mark.parametrize('service', ['Hermes-Team', 'Hermes-Owners', ''])
def test_cap_image_refuses_other_services(tmp_path, service):
    with pytest.raises(RuntimeError, match='Hermes-Cap'):
        bootstrap.seed(tmp_path, CAP, service=service)
    assert not (tmp_path / '.hermes').exists()
