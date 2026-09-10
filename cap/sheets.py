"""Cap's read-only Sheets CLI. Never prints credentials or upstream errors."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote, urlparse
import uuid

CREDENTIALS = Path('/data/cap/credentials/google-sheets.json')
DISABLED = Path('/data/cap/credentials/google-sheets.disabled')
SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
API = 'https://sheets.googleapis.com/v4/spreadsheets/'


class SheetsError(Exception):
    pass


def credentials(info):
    from google.oauth2.service_account import Credentials
    return Credentials.from_service_account_info(info, scopes=[SCOPE])


def provision(env=None, target=CREDENTIALS):
    """Materialize only the operator-approved service identity for the helper.

    Codex/terminal environment filters need not forward any private keys.
    The file is outside all repositories and is readable only by its owner.
    """
    env = os.environ if env is None else env
    if env.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap':
        return
    email = env.get('GOOGLE_SERVICE_ACCOUNT_EMAIL', '').strip()
    key = env.get('GOOGLE_SERVICE_API_KEY', '').strip().replace('\\n', '\n')
    if not email and not key:
        return
    if not re.fullmatch(r'[\w.+-]+@[\w.-]+\.iam\.gserviceaccount\.com', email) or not key:
        raise SheetsError('Google service-account configuration is incomplete or invalid.')
    info = {'type': 'service_account', 'client_email': email, 'private_key': key,
            'token_uri': 'https://oauth2.googleapis.com/token'}
    try:
        credentials(info)  # Validate the PEM before replacing our credential file.
    except Exception:
        raise SheetsError('Google service-account private key is invalid.') from None
    target = Path(target)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix='.google-sheets-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(info, stream)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def spreadsheet_id(value):
    if value.startswith('https://'):
        parsed = urlparse(value)
        match = re.match(r'^/spreadsheets/d/([A-Za-z0-9_-]+)(?:/|$)', parsed.path)
        if parsed.netloc != 'docs.google.com' or not match:
            raise SheetsError('Use a Google Sheets URL or spreadsheet ID.')
        value = match.group(1)
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', value):
        raise SheetsError('Use a Google Sheets URL or spreadsheet ID.')
    return value


def setup(env=None, target=CREDENTIALS, disabled=DISABLED):
    """A broken optional integration must not stop Slack or coding startup."""
    env = os.environ if env is None else env
    if env.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap':
        return
    disabled = Path(disabled)
    try:
        if not env.get('GOOGLE_SERVICE_ACCOUNT_EMAIL') or not env.get('GOOGLE_SERVICE_API_KEY'):
            raise SheetsError('Google Sheets credentials are missing.')
        provision(env, target)
    except Exception as exc:
        error_id = uuid.uuid4().hex[:12]
        disabled.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        disabled.write_text(error_id)
        print(json.dumps({'event': 'cap_sheets_setup_failed', 'error_id': error_id,
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return False
    if disabled.exists():
        disabled.unlink()
    print(json.dumps({'event': 'cap_sheets_setup', 'status': 'configured'}))
    return True


def read(sheet, a1=None, render='FORMATTED_VALUE', session=None):
    ident = spreadsheet_id(sheet)
    if render not in ('FORMATTED_VALUE', 'UNFORMATTED_VALUE', 'FORMULA'):
        raise SheetsError('Unsupported value rendering mode.')
    owned = session is None
    try:
        if owned:
            from google.auth.transport.requests import AuthorizedSession
            if DISABLED.exists():
                raise SheetsError('Cap Google Sheets setup failed; ask the operator to check cap_sheets_setup_failed in startup logs.')
            if not CREDENTIALS.is_file():
                raise SheetsError('Cap Google Sheets connection is missing; ask the operator to configure it.')
            session = AuthorizedSession(credentials(json.loads(CREDENTIALS.read_text())),
                                        refresh_timeout=20, max_refresh_attempts=1)
        url = API + ident
        if a1 is None:
            params = {'fields': 'spreadsheetId,properties(title),sheets(properties)'}
        else:
            if not a1 or len(a1) > 1000:
                raise SheetsError('Provide an A1 range such as Firm Financials!A1:E5.')
            url += '/values/' + quote(a1, safe='')
            params = {'valueRenderOption': render, 'dateTimeRenderOption': 'SERIAL_NUMBER'}
        response = session.get(url, params=params, timeout=30, allow_redirects=False)
        hints = {400: 'Invalid sheet range or request. List tabs with the metadata command.',
                 401: 'Google rejected authentication. Ask the operator to check the service-account key.',
                 403: 'Google denied access. Check spreadsheet sharing with the service account and Sheets API enablement.',
                 404: 'Spreadsheet not found or not shared with the service account.',
                 429: 'Google Sheets rate limit reached; retry later.'}
        if response.status_code != 200:
            raise SheetsError(hints.get(response.status_code, 'Google Sheets is unavailable; retry later.'))
        return response.json()
    finally:
        if owned and session is not None:
            session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only authenticated Google Sheets access for Cap.')
    parser.add_argument('action', choices=['metadata', 'read'])
    parser.add_argument('spreadsheet', help='Spreadsheet ID or Google Sheets URL')
    parser.add_argument('range', nargs='?', help='A1 range required for read')
    parser.add_argument('--render', choices=['FORMATTED_VALUE', 'UNFORMATTED_VALUE', 'FORMULA'],
                        default='FORMATTED_VALUE')
    args = parser.parse_args(argv)
    if (args.action == 'read') != (args.range is not None):
        parser.error('read requires a range; metadata takes no range')
    try:
        print(json.dumps({'ok': True, 'data': read(args.spreadsheet, args.range, args.render)}))
        return 0
    except Exception as exc:
        error_id = uuid.uuid4().hex[:12]
        hint = str(exc) if isinstance(exc, SheetsError) else 'Google Sheets authentication or network request failed. Ask the operator to check the connection.'
        print(json.dumps({'ok': False, 'error_id': error_id, 'error': hint}))
        print(json.dumps({'event': 'cap_sheets_failed', 'error_id': error_id,
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
