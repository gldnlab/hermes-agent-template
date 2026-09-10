import asyncio
import sys
from types import SimpleNamespace as NS

import pytest

from test_cap_routing import KB, enqueue, load

routing, patch = load('routing'), load('patch_routing')


@pytest.fixture
def router(tmp_path):
    return routing.Router(KB(tmp_path), tmp_path)


def test_one_intro_per_thread_and_no_followup_ack(router):
    enqueue(router)
    enqueue(router, message='1788976795.031949')
    with router.connect('vw-site') as conn:
        rows = conn.execute('SELECT ack FROM cap_messages ORDER BY seq').fetchall()
        assert 'https://' in rows[0]['ack']
        assert rows[1]['ack'] == ''


@pytest.mark.parametrize('status,run,outcome,ended,error,expected', [
    ('ready', None, None, None, None, 'hourglass_flowing_sand'),
    ('running', 1, None, None, None, 'eyes'),
    ('running', None, None, None, None, 'hourglass_flowing_sand'),
    ('review', 1, 'review_requested', 1, None, 'white_check_mark'),
    ('running', 1, 'review_requested', 1, None, 'white_check_mark'),
    ('done', 1, 'completed', 1, None, 'white_check_mark'),
    ('blocked', 1, 'blocked', 1, None, 'x'),
    ('ready', 1, 'crashed', 1, None, 'x'),
    ('blocked', None, None, None, 'error123', 'x'),
])
def test_reactions_follow_each_messages_run(status, run, outcome, ended, error, expected):
    assert routing.desired_reaction(dict(status=status, run_id=run, outcome=outcome,
        ended_at=ended, error_id=error)) == expected


class SlackError(Exception):
    def __init__(self, code):
        self.response = {'error': code}


class Client:
    def __init__(self):
        self.reactions = set()
        self.calls = 0
        self.fail = False

    async def reactions_add(self, **args):
        self.calls += 1
        if self.fail:
            raise SlackError('missing_scope')
        key = args['timestamp'], args['name']
        if key in self.reactions:
            raise SlackError('already_reacted')
        self.reactions.add(key)
        return {'ok': True}

    async def reactions_remove(self, **args):
        self.calls += 1
        key = args['timestamp'], args['name']
        if key not in self.reactions:
            raise SlackError('no_reaction')
        self.reactions.remove(key)
        return {'ok': True}


def adapter(client):
    return NS(_get_client=lambda *args, **kw: client, _reactions_enabled=lambda: True)


def test_reaction_operations_are_idempotent_and_replace_status():
    client = Client()
    for desired in ['eyes', 'eyes', 'white_check_mark', 'white_check_mark']:
        asyncio.run(routing.set_reaction(adapter(client), 'C0BV1CCCPS8', '1788976794.031949', desired))
    assert client.reactions == {('1788976794.031949', 'white_check_mark')}


def test_preparation_hold_is_queued_not_failure():
    assert routing.desired_reaction(dict(status='blocked', run_id=None, outcome=None,
        ended_at=None, error_id=None, routing_state='preparing')) == 'hourglass_flowing_sand'


def test_reaction_failure_is_persistently_retried_and_not_repeated_when_successful(router):
    enqueue(router)
    client = Client()
    gateway = NS(adapters={'slack': adapter(client)})
    client.fail = True
    asyncio.run(routing.update_feedback(gateway, router, 'vw-site'))
    with router.connect('vw-site') as conn, conn:
        row = conn.execute('SELECT * FROM cap_feedback').fetchone()
        assert row['reaction'] is None and row['failures'] == 1 and row['next_attempt'] > 0
        conn.execute('UPDATE cap_feedback SET next_attempt=0')
    client.fail = False
    asyncio.run(routing.update_feedback(gateway, router, 'vw-site'))
    count = client.calls
    # Recreating the router simulates restart: delivered status isn't reposted.
    asyncio.run(routing.update_feedback(gateway, routing.Router(router.kb, router.root), 'vw-site'))
    assert client.calls == count


def test_full_answer_is_from_exact_event_run_not_latest_and_preserves_end(router, monkeypatch):
    enqueue(router)
    monkeypatch.setenv('RAILWAY_SERVICE_NAME', 'Hermes-Cap')
    monkeypatch.setattr(routing, 'ROOT', router.root)
    monkeypatch.setitem(sys.modules, 'hermes_cli', NS(kanban_db=router.kb))
    answer = 'Answer\n' + 'details ' * 1500 + '\nhttps://preview.example.test/actual\nFINAL CAVEAT'
    with router.connect('vw-site') as conn, conn:
        task = conn.execute('SELECT task_id FROM cap_threads').fetchone()[0]
        conn.execute('INSERT INTO task_runs VALUES(1,?,?,?,1)', (task, answer, 'review_requested'))
        conn.execute('INSERT INTO task_runs VALUES(2,?,?,?,2)', (task, 'Wrong later answer', 'review_requested'))
    sub = dict(task_id=task, platform='slack', chat_id='C0BV1CCCPS8', thread_id='1788976794.031949')
    event = NS(kind='review_requested', run_id=1, payload={'summary': 'Truncated'})
    rendered = routing.full_notification('vw-site', sub, event, 'old notification')
    assert answer in rendered and 'Wrong later answer' not in rendered
    for service in ('Hermes-Team', 'Hermes-Owners'):
        monkeypatch.setenv('RAILWAY_SERVICE_NAME', service)
        assert routing.full_notification('vw-site', sub, event, 'old notification') == 'old notification'


def test_feedback_patches_are_narrow_and_fail_on_drift():
    source = ('async def on_processing_start(self, event):\n'
              '        """Add an in-progress reaction when message processing begins."""\n'
              'async def on_processing_complete(self, event, outcome):\n'
              '        """Swap the in-progress reaction for a final success/failure reaction."""\n')
    patched = patch.patch_slack_feedback(source)
    compile(patched, '<slack>', 'exec')
    assert patched.count('if owns_event(event):') == 2
    with pytest.raises(RuntimeError):
        patch.patch_slack_feedback(patched)
    source = 'async def test():\n                            _send_res = await adapter.send(\n                                "test")\n'
    result = patch.patch_notifier(source)
    compile(result, '<notifier>', 'exec')
    assert 'full_notification(board_slug, sub, ev, msg)' in result
    with pytest.raises(RuntimeError):
        patch.patch_notifier(result)
