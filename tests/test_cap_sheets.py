import importlib.util
import json
from pathlib import Path
import stat

import pytest

spec = importlib.util.spec_from_file_location('cap_sheets', Path(__file__).parents[1] / 'cap/sheets.py')
sheets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sheets)
ID = 'a' * 40


class Session:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return type('Response', (), {'status_code': self.status,
                                    'json': lambda _: {'values': [['Year', 'Date']]}})()


def test_provision_only_cap_and_only_two_credentials(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sheets, 'credentials', lambda info: seen.append(info))
    target = tmp_path / 'credentials/google-sheets.json'
    env = {'RAILWAY_SERVICE_NAME': 'Hermes-Team', 'GOOGLE_SERVICE_ACCOUNT_EMAIL': 'test@project.iam.gserviceaccount.com',
           'GOOGLE_SERVICE_API_KEY': 'test\\nkey', 'SLACK_BOT_TOKEN': 'not-allowed'}
    sheets.provision(env, target)
    assert not target.exists()
    sheets.provision({**env, 'RAILWAY_SERVICE_NAME': 'Hermes-Cap'}, target)
    data = json.loads(target.read_text())
    assert data['private_key'] == 'test\nkey'
    assert 'not-allowed' not in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert len(seen) == 1
    assert list(target.parent.iterdir()) == [target]


def test_invalid_provision_preserves_existing_file(tmp_path, monkeypatch):
    target = tmp_path / 'existing'
    target.write_text('preserved')
    def invalid(info):
        raise ValueError('SECRET RAW KEY')
    monkeypatch.setattr(sheets, 'credentials', invalid)
    env = {'RAILWAY_SERVICE_NAME': 'Hermes-Cap', 'GOOGLE_SERVICE_ACCOUNT_EMAIL': 'test@project.iam.gserviceaccount.com',
           'GOOGLE_SERVICE_API_KEY': 'SECRET RAW KEY'}
    with pytest.raises(sheets.SheetsError, match='private key is invalid') as error:
        sheets.provision(env, target)
    assert 'SECRET' not in str(error.value)
    assert target.read_text() == 'preserved'


def test_read_is_get_only_fixed_host_encoded_range():
    session = Session()
    result = sheets.read('https://docs.google.com/spreadsheets/d/' + ID + '/edit#gid=12',
                         "'Firm Financials'!A1:E5", 'FORMULA', session)
    url, kwargs = session.calls[0]
    assert url == sheets.API + ID + '/values/%27Firm%20Financials%27%21A1%3AE5'
    assert kwargs['params']['valueRenderOption'] == 'FORMULA'
    assert kwargs['allow_redirects'] is False
    assert kwargs['timeout'] == 30
    assert result['values'][0] == ['Year', 'Date']


def test_metadata_avoids_full_grid_download():
    session = Session()
    sheets.read(ID, session=session)
    url, kwargs = session.calls[0]
    assert url == sheets.API + ID
    assert kwargs['params'] == {'fields': 'spreadsheetId,properties(title),sheets(properties)'}


@pytest.mark.parametrize('value', ['https://evil.test/spreadsheets/d/' + ID, '../secret', 'https://docs.google.com@evil.test/' + ID])
def test_rejects_untrusted_destinations(value):
    with pytest.raises(sheets.SheetsError):
        sheets.read(value, session=Session())


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429, 500, 302])
def test_actionable_failure_without_response_body(status):
    with pytest.raises(sheets.SheetsError):
        sheets.read(ID, session=Session(status))


def test_cli_redacts_unexpected_errors(capsys, monkeypatch):
    def fail(*args):
        raise RuntimeError('SECRET TOKEN')
    monkeypatch.setattr(sheets, 'read', fail)
    assert sheets.main(['metadata', ID]) == 1
    output = capsys.readouterr()
    assert 'SECRET' not in output.out + output.err
    assert json.loads(output.out)['error_id'] == json.loads(output.err)['error_id']


def test_cli_has_no_write_action():
    with pytest.raises(SystemExit):
        sheets.main(['update', ID, 'A1'])


def test_read_only_scope_and_no_delegation(monkeypatch):
    import sys
    from types import ModuleType
    seen = {}
    class Credentials:
        @staticmethod
        def from_service_account_info(info, **kwargs):
            seen.update(kwargs)
    service = ModuleType('google.oauth2.service_account')
    service.Credentials = Credentials
    monkeypatch.setitem(sys.modules, 'google.oauth2.service_account', service)
    sheets.credentials({})
    assert seen == {'scopes': ['https://www.googleapis.com/auth/spreadsheets.readonly']}


def test_setup_failure_blocks_sheets_not_gateway(tmp_path, monkeypatch, capsys):
    disabled = tmp_path / 'google-sheets.disabled'
    env = {'RAILWAY_SERVICE_NAME': 'Hermes-Cap', 'GOOGLE_SERVICE_ACCOUNT_EMAIL': 'test',
           'GOOGLE_SERVICE_API_KEY': 'secret'}
    def fail(*args):
        raise RuntimeError('SECRET KEY')
    monkeypatch.setattr(sheets, 'provision', fail)
    assert sheets.setup(env, tmp_path / 'key.json', disabled) is False
    assert disabled.exists()
    assert 'SECRET KEY' not in capsys.readouterr().err
    monkeypatch.setattr(sheets, 'provision', lambda *a: None)
    assert sheets.setup(env, tmp_path / 'key.json', disabled) is True
    assert not disabled.exists()
