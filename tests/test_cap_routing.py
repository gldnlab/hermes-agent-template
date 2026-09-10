import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace as NS

import pytest

CAP = Path(__file__).parents[1] / 'cap'


def load(name):
    spec = importlib.util.spec_from_file_location(name, CAP / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


routing, patch = load('routing'), load('patch_routing')


class KB:
    """Small native API double; integration smoke also runs against live Hermes."""
    def __init__(self, root):
        self.root = root

    def connect(self, board):
        conn = sqlite3.connect(self.root / (board + '.db'), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, status TEXT, current_run_id TEXT, path TEXT, branch TEXT, idem TEXT);
            CREATE TABLE IF NOT EXISTS comments(task TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS subs(task TEXT UNIQUE);''')
        return conn

    @contextmanager
    def write_txn(self, conn):
        with conn:
            yield

    def create_task(self, conn, **args):
        old = conn.execute('SELECT id FROM tasks WHERE idem=?', (args['idempotency_key'],)).fetchone()
        if old:
            return old['id']
        task = 't_' + str(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] + 1)
        with conn:
            conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)', (task, args['initial_status'], None,
                args['workspace_path'], args['branch_name'], args['idempotency_key']))
        return task

    def get_task(self, conn, task):
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        return NS(**dict(row), claim_lock=row['current_run_id'] if row['status'] == 'running' else None) if row else None

    def add_comment(self, conn, task, author, body):
        conn.execute('INSERT INTO comments VALUES(?,?)', (task, body))

    def add_notify_sub(self, conn, **args):
        with conn:
            conn.execute('INSERT OR IGNORE INTO subs VALUES(?)', (args['task_id'],))

    def _append_event(self, *args):
        pass

    def promote_task(self, conn, task, **args):
        with conn:
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
        return True, None

    def reopen_review_task(self, conn, task):
        with conn:
            return conn.execute("UPDATE tasks SET status='ready' WHERE id=? AND status='review'",
                                (task,)).rowcount == 1

    def unblock_task(self, conn, task):
        return self.promote_task(conn, task)[0]


@pytest.fixture
def router(tmp_path):
    return routing.Router(KB(tmp_path), tmp_path)


def enqueue(router, thread='1788976794.031949', message='1788976794.031949', **kw):
    return router.enqueue('C0BV1CCCPS8', thread, message, 'U013H8QQBGT', 'test request', **kw)


def test_duplicate_and_concurrent_delivery_create_one_task(router):
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: enqueue(router), range(8)))
    with router.connect('vw-site') as conn:
        for table in ('tasks', 'comments', 'cap_threads', 'cap_messages', 'subs'):
            assert conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
        assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'blocked'


def test_distinct_threads_and_channels_are_separate(router):
    enqueue(router)
    enqueue(router, thread='1788976795.031949', message='1788976795.031949')
    router.enqueue('C0BUTAUA88M', '1788976794.031949', '1788976794.031949', 'U013H8QQBGT', 'hq request')
    with router.connect('vw-site') as conn:
        rows = conn.execute('SELECT path,branch FROM cap_threads').fetchall()
        assert len(rows) == 2 and rows[0]['path'] != rows[1]['path']
        assert rows[0]['branch'] != rows[1]['branch']
    with router.connect('vw-hq') as conn:
        assert '/repos/vw-hq/' in conn.execute('SELECT path FROM cap_threads').fetchone()[0]


def test_followup_durable_and_not_dispatched_concurrently(router, monkeypatch):
    enqueue(router)
    with router.connect('vw-site') as conn, conn:
        conn.execute("UPDATE cap_threads SET state='active', consumed=1")
        conn.execute("UPDATE tasks SET status='running', current_run_id='run1'")
    enqueue(router, message='1788976796.031949')
    monkeypatch.setattr(router, 'validate', lambda *args: None)
    router.reconcile('vw-site')
    with router.connect('vw-site') as conn, conn:
        assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'running'
        conn.execute("UPDATE tasks SET status='review', current_run_id=NULL")
    # New Router simulates gateway restart; no in-memory queue required.
    replacement = routing.Router(router.kb, router.root)
    monkeypatch.setattr(replacement, 'validate', lambda *args: None)
    replacement.reconcile('vw-site')
    with router.connect('vw-site') as conn:
        assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'ready'
        assert conn.execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 2


def test_failed_provisioning_is_blocked_and_visible(router, monkeypatch):
    enqueue(router)
    monkeypatch.setattr(routing, 'git', lambda *args: 'wrong-repo')
    router.reconcile('vw-site')
    with router.connect('vw-site') as conn:
        row = conn.execute('SELECT * FROM cap_threads').fetchone()
        assert row['error_id'] and row['state'] == 'preparing'
        assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'blocked'
        assert row['error_id'] in conn.execute('SELECT ack FROM cap_messages').fetchone()[0]


def test_message_comment_roll_back_together(router, monkeypatch):
    def fail(conn, *args):
        conn.execute("INSERT INTO comments VALUES('test','must rollback')")
        raise RuntimeError('test failure')
    monkeypatch.setattr(router.kb, 'add_comment', fail)
    with pytest.raises(RuntimeError):
        enqueue(router)
    with router.connect('vw-site') as conn:
        assert conn.execute('SELECT COUNT(*) FROM cap_messages').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 0


def test_attachments_persist_and_missing_attachments_fail(router, tmp_path):
    attachment = tmp_path / 'image.png'
    attachment.write_bytes(b'image fixture')
    enqueue(router, media=[str(attachment)])
    with router.connect('vw-site') as conn:
        content = json.loads(conn.execute('SELECT text FROM cap_messages').fetchone()[0])
        copy = Path(content['attachments'][0])
        assert copy != attachment and copy.read_bytes() == attachment.read_bytes()
    with pytest.raises(ValueError):
        enqueue(router, message='1788976797.031949', media=['/missing/image.png'])


@pytest.mark.parametrize('service,channel,scope,authorized,expected', [
    ('Hermes-Team', 'C0BV1CCCPS8', routing.WORKSPACE, True, False),
    ('Hermes-Owners', 'C0BV1CCCPS8', routing.WORKSPACE, True, False),
    ('Hermes-Cap', 'D0C0HLU9HL2', routing.WORKSPACE, True, False),
    ('Hermes-Cap', 'C0BV1CCCPS8', 'wrong-team', True, True),
    ('Hermes-Cap', 'C0BV1CCCPS8', routing.WORKSPACE, False, True),
])
def test_service_dm_and_authorization_gates(monkeypatch, service, channel, scope, authorized, expected):
    monkeypatch.setenv('RAILWAY_SERVICE_NAME', service)
    event = NS(source=NS(platform='slack', chat_id=channel, scope_id=scope))
    gateway = NS(_is_user_authorized_for_source=lambda _: authorized)
    assert asyncio.run(routing.dispatch(event, gateway)) is expected


def test_native_patch_fails_on_drift_and_contains_no_alternative_runner():
    source = ('async def test():\n        logger.info("Starting Hermes Gateway...")\n'
              '        # Fire pre_gateway_dispatch plugin hook for user-originated messages.\n')
    changed = patch.patch_gateway(source)
    compile(changed, '<fixture>', 'exec')
    assert 'await cap_dispatch(event, self)' in changed
    with pytest.raises(RuntimeError):
        patch.patch_gateway(changed)
    with pytest.raises(RuntimeError):
        patch.patch_gateway('changed upstream')
    worker = patch.patch_kanban('def test():\n    prompt = f"work kanban task {task.id}"\n')
    assert 'worker_prompt(task, workspace, board)' in worker
    compile(worker, '<fixture>', 'exec')
