"""Cap-only Slack ingress for native Hermes Kanban (no second coding runner).

The board database owns both task state and the small routing/delivery ledger.
Native Kanban owns claims, worker execution, retries, results and notifications.
"""
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

WORKSPACE = 'TLREWAM8X'
ROUTES = {'C0BUZL57K97': 'vw-crm', 'C0BUTAUA88M': 'vw-hq',
          'C0BUTATHDAR': 'vw-dashboards', 'C0BV1CCCPS8': 'vw-site'}
ROOT = Path('/data/cap')
LOG = logging.getLogger('cap.routing')
STAMP = re.compile(r'^\d{10,}\.\d{6}$')


def emit(event, **fields):
    # No prompts, command output, credentials, or raw exception text in logs.
    LOG.warning(json.dumps({'event': event, **fields}, sort_keys=True))


def error_hint(exc):
    # Expose only our controlled diagnostic vocabulary, never upstream output.
    detail = str(exc)
    known = ('Repository origin changed', 'Mapped branch changed',
             'Worktree belongs to another repository', 'Mapped workspace escaped repository',
             'Mapped task missing', 'Existing branch has unexpected commits',
             'Preparing worktree no longer matches recorded base',
             'Native task promotion refused', 'Task is closed; start a new Slack thread',
             'Missing stable Slack identity', 'Attachment unavailable or too large')
    if detail in known or detail in ('git operation failed: fetch', 'git operation failed: worktree'):
        return detail
    return 'Integration check failed (' + type(exc).__name__ + ')'


def schema(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS cap_threads (
        route_key TEXT PRIMARY KEY, channel TEXT NOT NULL, thread TEXT NOT NULL,
        task_id TEXT UNIQUE NOT NULL, path TEXT NOT NULL, branch TEXT NOT NULL,
        base_sha TEXT, state TEXT NOT NULL DEFAULT 'preparing',
        consumed INTEGER NOT NULL DEFAULT 0, error_id TEXT);
      CREATE TABLE IF NOT EXISTS cap_messages (
        seq INTEGER PRIMARY KEY, route_key TEXT NOT NULL, message_id TEXT NOT NULL,
        user_id TEXT NOT NULL, text TEXT NOT NULL, ack TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0,
        UNIQUE(route_key, message_id));
      CREATE TABLE IF NOT EXISTS cap_feedback (
        seq INTEGER PRIMARY KEY, run_id INTEGER, reaction TEXT,
        next_attempt REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0);
    ''')


@contextmanager
def locked(root, board):
    root.mkdir(parents=True, exist_ok=True)
    with (root / (board + '.routing.lock')).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True,
                            text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('git operation failed: ' + args[0])
    return result.stdout.strip()


class Router:
    def __init__(self, kb, root=ROOT):
        self.kb, self.root = kb, Path(root)

    @contextmanager
    def connect(self, board):
        conn = self.kb.connect(board=board)
        try:
            schema(conn)
            yield conn
        finally:
            conn.close()

    def enqueue(self, channel, thread, message, user, text, media=(), context=''):
        board = ROUTES[channel]
        if not STAMP.fullmatch(thread) or not STAMP.fullmatch(message) or not user:
            raise ValueError('Missing stable Slack identity')
        key = hashlib.sha256(f'{WORKSPACE}/{channel}/{thread}'.encode()).hexdigest()
        with locked(self.root, board), self.connect(board) as conn:
            previous = conn.execute('SELECT * FROM cap_messages WHERE route_key=? AND message_id=?',
                                    (key, message)).fetchone()
            if previous:
                return board, key
            mapping = conn.execute('SELECT * FROM cap_threads WHERE route_key=?', (key,)).fetchone()
            if mapping is None:
                path = self.root / 'repos' / board / '.worktrees' / ('slack-' + key[:20])
                branch = f'cap/slack-{channel.lower()}-{thread.replace(".", "-")}'
                # Blocked until workspace and notification subscription exist.
                # The per-board lock also closes native create_task's idempotency race.
                with self.kb.write_txn(conn):
                    task = self.kb.create_task(conn, title=text.strip()[:160] or 'Slack attachment',
                        body=('This task is owned by the Cap Slack router. Work only in its mapped '
                          'repository/worktree; read all Slack request comments. Do not create '
                          'another task for this thread. Proposals require approval before edits. '
                          'Return results through kanban_request_review, including PR, preview '
                          'and checks. Never merge without Derek approving the exact PR/commit.'),
                        assignee='default', created_by='cap-slack-router', workspace_kind='worktree',
                        workspace_path=str(path), branch_name=branch, initial_status='blocked',
                        idempotency_key='cap-slack:' + key, board=board, reasoning_effort='medium')
                    # Native recompute_ready promotes bare 'blocked' rows. Its
                    # explicit operator-block event is required to hold ingress.
                    self.kb._append_event(conn, task, 'blocked',
                                          {'reason': 'Cap routing preparation', 'source_status': 'ready'})
                    conn.execute('INSERT INTO cap_threads(route_key,channel,thread,task_id,path,branch) '
                                 'VALUES(?,?,?,?,?,?)', (key, channel, thread, task, str(path), branch))
                mapping = conn.execute('SELECT * FROM cap_threads WHERE route_key=?', (key,)).fetchone()
            task = self.kb.get_task(conn, mapping['task_id'])
            if task is None or task.status in ('done', 'archived'):
                raise ValueError('Task is closed; start a new Slack thread')
            # Copy adapter-downloaded attachments off ephemeral storage before accepting.
            attachments = []
            for index, name in enumerate(media):
                src = Path(name)
                if not src.is_file() or src.is_symlink() or src.stat().st_size > 50 * 1024 * 1024:
                    raise ValueError('Attachment unavailable or too large')
                dest = self.root / 'attachments' / key / message / (str(index) + src.suffix)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                attachments.append(str(dest))
            content = json.dumps({'slack_message': message, 'user': user, 'request': text,
                                  'context': context, 'attachments': attachments}, ensure_ascii=False)
            first = not conn.execute('SELECT 1 FROM cap_messages WHERE route_key=? LIMIT 1', (key,)).fetchone()
            ack = (f'Tracking this thread in *{board}* · `{task.id}`.\n'
                   'https://hermes-cap-production.up.railway.app/kanban') if first else ''
            # Comment + inbound dedup marker commit together, or neither does.
            with self.kb.write_txn(conn):
                self.kb.add_comment(conn, task.id, 'slack:' + user, content)
                conn.execute('INSERT INTO cap_messages(route_key,message_id,user_id,text,ack) '
                             'VALUES(?,?,?,?,?)', (key, message, user, content, ack))
                seq = conn.execute('SELECT seq FROM cap_messages WHERE route_key=? AND message_id=?',
                                   (key, message)).fetchone()[0]
                conn.execute('INSERT INTO cap_feedback(seq) VALUES(?)', (seq,))
            self.kb.add_notify_sub(conn, task_id=task.id, platform='slack', chat_id=channel,
                thread_id=thread, user_id=user, chat_type='group', notifier_profile='default',
                delivery_mode='notify', delivery_metadata={'slack_team_id': WORKSPACE})
            emit('cap_request_saved', board=board, task_id=task.id, message_id=message)
        return board, key

    def validate(self, board, row):
        repo = self.root / 'repos' / board
        path = Path(row['path'])
        if not path.resolve().is_relative_to(repo.resolve() / '.worktrees'):
            raise ValueError('Mapped workspace escaped repository')
        if git(repo, 'remote', 'get-url', 'origin') != f'https://github.com/gldnlab/{board}.git':
            raise ValueError('Repository origin changed')
        if git(path, 'branch', '--show-current') != row['branch']:
            raise ValueError('Mapped branch changed')
        if Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != (repo / '.git').resolve():
            raise ValueError('Worktree belongs to another repository')

    def reconcile(self, board):
        with locked(self.root, board), self.connect(board) as conn:
            for row in conn.execute('SELECT * FROM cap_threads').fetchall():
                try:
                    task = self.kb.get_task(conn, row['task_id'])
                    if not task:
                        raise ValueError('Mapped task missing')
                    if row['state'] == 'preparing':
                        message = conn.execute('SELECT * FROM cap_messages WHERE route_key=? ORDER BY seq LIMIT 1',
                                               (row['route_key'],)).fetchone()
                        if not message:
                            continue  # Incomplete ingress must not provision a workspace.
                        repo, path = self.root / 'repos' / board, Path(row['path'])
                        if git(repo, 'remote', 'get-url', 'origin') != f'https://github.com/gldnlab/{board}.git':
                            raise ValueError('Repository origin changed')
                        base = row['base_sha']
                        if not base:
                            git(repo, 'fetch', 'origin', 'main')
                            base = git(repo, 'rev-parse', 'FETCH_HEAD^{commit}')
                            with conn:
                                conn.execute('UPDATE cap_threads SET base_sha=? WHERE route_key=?', (base, row['route_key']))
                        if not path.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            exists = subprocess.run(['git', '-C', str(repo), 'show-ref', '--verify', '--quiet',
                                                     'refs/heads/' + row['branch']]).returncode == 0
                            if exists:
                                # Recover the branch-created/worktree-not-created crash window only.
                                if git(repo, 'rev-parse', row['branch']) != base:
                                    raise ValueError('Existing branch has unexpected commits')
                                git(repo, 'worktree', 'add', str(path), row['branch'])
                            else:
                                git(repo, 'worktree', 'add', '-b', row['branch'], str(path), base)
                        self.validate(board, row)
                        if git(path, 'rev-parse', 'HEAD') != base:
                            raise ValueError('Preparing worktree no longer matches recorded base')
                        # First inbound may have crashed between comment and subscription.
                        self.kb.add_notify_sub(conn, task_id=task.id, platform='slack', chat_id=row['channel'],
                            thread_id=row['thread'], user_id=message['user_id'], chat_type='group',
                            notifier_profile='default', delivery_mode='notify',
                            delivery_metadata={'slack_team_id': WORKSPACE})
                        promoted = self.kb.unblock_task(conn, task.id)
                        if not promoted and task.status not in ('ready', 'running'):
                            raise RuntimeError('Native task promotion refused')
                        with conn:
                            conn.execute("UPDATE cap_threads SET state='active',error_id=NULL WHERE route_key=?", (row['route_key'],))
                        emit('cap_task_ready', board=board, task_id=task.id, base_sha=base)
                    elif row['state'] == 'active':
                        pending = conn.execute('SELECT 1 FROM cap_messages WHERE route_key=? AND seq>? LIMIT 1',
                                               (row['route_key'], row['consumed'])).fetchone()
                        if pending and task.status == 'review' and not task.claim_lock:
                            self.validate(board, row)
                            if self.kb.reopen_review_task(conn, task.id):
                                emit('cap_followup_ready', board=board, task_id=task.id)
                        elif pending and task.status == 'blocked':
                            latest = conn.execute('SELECT text FROM cap_messages WHERE route_key=? ORDER BY seq DESC LIMIT 1',
                                                  (row['route_key'],)).fetchone()
                            request = re.sub(r'<@[A-Z0-9]+>', '', json.loads(latest['text'])['request']).strip().lower()
                            if request in ('retry', 'retry task'):
                                self.validate(board, row)
                                self.kb.unblock_task(conn, task.id)
                except Exception as exc:
                    error = row['error_id'] or uuid.uuid4().hex[:12]
                    if not row['error_id']:
                        with conn:
                            conn.execute('UPDATE cap_threads SET error_id=? WHERE route_key=?', (error, row['route_key']))
                            conn.execute('UPDATE cap_messages SET ack=?, delivered=0 WHERE seq=(SELECT MAX(seq) '
                                         'FROM cap_messages WHERE route_key=?)',
                                (f'Cap could not prepare `{row["task_id"]}`. Request saved; no coding fallback. '
                                 f'{error_hint(exc)}. Error `{error}` is in Hermes-Cap logs.', row['route_key']))
                    emit('cap_route_failed', error_id=error, task_id=row['task_id'], reason=error_hint(exc))


def worker_prompt(task, workspace, board):
    """Called by native dispatcher before spawning; never changes model/runtime."""
    if os.environ.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap' or board not in ROUTES.values():
        return ''
    from hermes_cli import kanban_db as kb
    router = Router(kb, ROOT)
    with locked(ROOT, board), router.connect(board) as conn:
        row = conn.execute('SELECT * FROM cap_threads WHERE task_id=?', (task.id,)).fetchone()
        if row is None:
            return ''  # Existing operator-created native tasks are still supported.
        if row['state'] != 'active' or Path(workspace).resolve() != Path(row['path']).resolve():
            raise ValueError('Cap task workspace is not ready')
        router.validate(board, row)
        messages = conn.execute('SELECT seq,text FROM cap_messages WHERE route_key=? ORDER BY seq',
                                (row['route_key'],)).fetchall()
        if not messages:
            raise ValueError('Routed worker has no durable request')
        # Snapshot is conservative: a later message causes another native turn,
        # even if this worker happens to read it via kanban_show in the meantime.
        with conn:
            conn.execute('UPDATE cap_threads SET consumed=? WHERE route_key=?',
                         (messages[-1]['seq'], row['route_key']))
            # Bind each accepted message to the actual native run. Failed runs
            # can be retried; successful earlier messages must stay completed.
            conn.execute('UPDATE cap_feedback SET run_id=?, next_attempt=0 WHERE seq IN '
                '(SELECT seq FROM cap_messages WHERE route_key=? AND seq<=?) AND '
                '(run_id IS NULL OR run_id IN (SELECT id FROM task_runs WHERE '
                "outcome IS NULL OR outcome NOT IN ('review_requested','completed')))",
                (task.current_run_id, row['route_key'], messages[-1]['seq']))
        latest = messages[-1]['text']
        return ('\n\nThis is a routed Slack task. Read kanban_show and its comments for full history. '
                'For private Google source sheets use python3 /app/cap/sheets.py metadata SHEET_ID '
                'or python3 /app/cap/sheets.py read SHEET_ID A1_RANGE; authenticated read-only access is installed. '
                'Never read or print credentials. '
                'Respond to the latest request below; do not repeat completed work. '
                'Use the mapped workspace only. Finish with kanban_request_review with your answer, '
                'PR and preview where applicable. A question or proposal is not permission to edit. '
                'Do not create another task. No merge without Derek approving the exact PR/commit.\n'
                + latest)


def explicit_request(conn, task_id):
    """Registered requests may refine an existing PR; don't treat them as duplicates."""
    if os.environ.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap':
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cap_threads'").fetchone():
        return False
    return bool(conn.execute("SELECT 1 FROM cap_threads t WHERE t.task_id=? AND t.state='active' "
        'AND EXISTS(SELECT 1 FROM cap_messages m WHERE m.route_key=t.route_key)', (task_id,)).fetchone())


def owns_event(event):
    source = getattr(event, 'source', None)
    return (os.environ.get('RAILWAY_SERVICE_NAME') == 'Hermes-Cap'
            and getattr(source, 'scope_id', None) == WORKSPACE
            and getattr(source, 'chat_id', None) in ROUTES
            and str(getattr(getattr(source, 'platform', None), 'value', getattr(source, 'platform', None))) == 'slack')


def full_notification(board, sub, event, original):
    """Keep native delivery/retry ownership, but use the event's FULL run result."""
    if (os.environ.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap' or board not in ROUTES.values()
            or sub.get('platform') != 'slack' or event.kind not in ('review_requested', 'completed')):
        return original
    from hermes_cli import kanban_db as kb
    with Router(kb, ROOT).connect(board) as conn:
        route = conn.execute('SELECT * FROM cap_threads WHERE task_id=?', (sub['task_id'],)).fetchone()
        if not route or route['channel'] != sub['chat_id'] or route['thread'] != sub.get('thread_id'):
            return original
        run = conn.execute('SELECT summary FROM task_runs WHERE id=? AND task_id=?',
                           (getattr(event, 'run_id', None), sub['task_id'])).fetchone()
        answer = (run['summary'] if run else None) or (event.payload or {}).get('summary')
        if not answer:
            return original
        label = 'Ready for your review' if event.kind == 'review_requested' else 'Done'
        return str(answer).strip() + f'\n\n_{label} · `{sub["task_id"]}`_'


REACTIONS = ('hourglass_flowing_sand', 'eyes', 'white_check_mark', 'x')


def desired_reaction(row):
    row = dict(row)
    if row['outcome'] in ('review_requested', 'completed'):
        return 'white_check_mark'
    if row['error_id']:
        return 'x'
    if row.get('routing_state') == 'preparing':
        return 'hourglass_flowing_sand'
    if row['status'] in ('blocked', 'failed', 'cancelled', 'triage'):
        return 'x'
    if row['run_id'] is not None:
        if row['ended_at'] is not None:
            return 'x'
        if row['status'] == 'running':
            return 'eyes'
    return 'hourglass_flowing_sand'


def slack_adapter(gateway):
    for platform, adapter in gateway.adapters.items():
        if str(getattr(platform, 'value', platform)) == 'slack':
            return adapter
    raise RuntimeError('Slack adapter unavailable')


async def set_reaction(adapter, channel, message, desired):
    client = adapter._get_client(channel, team_id=WORKSPACE)
    # Direct native client calls preserve idempotent Slack error codes instead
    # of the adapter helpers' ambiguous False for both duplicates and failures.
    for operation, emoji in [('add', desired)] + [('remove', e) for e in REACTIONS if e != desired]:
        try:
            result = await getattr(client, 'reactions_' + operation)(channel=channel, timestamp=message, name=emoji)
            if result.get('ok') is False:
                raise RuntimeError('Slack rejected reaction')
        except Exception as exc:
            response = getattr(exc, 'response', {})
            code = response.get('error') if hasattr(response, 'get') else None
            if (operation, code) in (('add', 'already_reacted'), ('remove', 'no_reaction')):
                continue
            raise


async def update_feedback(gateway, router, board):
    adapter = slack_adapter(gateway)
    if not adapter._reactions_enabled():
        return
    with router.connect(board) as conn:
        # Adopt only the newest pre-upgrade request in each thread, not every
        # historical message. Restarting never replays old textual answers.
        with conn:
            conn.execute('INSERT OR IGNORE INTO cap_feedback(seq,run_id) '
                'SELECT m.seq,CASE WHEN m.seq<=t.consumed THEN '
                '(SELECT MAX(id) FROM task_runs WHERE task_id=t.task_id) ELSE NULL END '
                'FROM cap_threads t JOIN cap_messages m ON m.seq='
                '(SELECT MAX(seq) FROM cap_messages WHERE route_key=t.route_key)')
        rows = conn.execute('SELECT f.*,m.message_id,t.channel,t.error_id,t.state AS routing_state,k.status,r.outcome,r.ended_at '
            'FROM cap_feedback f JOIN cap_messages m ON m.seq=f.seq '
            'JOIN cap_threads t ON t.route_key=m.route_key JOIN tasks k ON k.id=t.task_id '
            'LEFT JOIN task_runs r ON r.id=f.run_id WHERE f.next_attempt<=?', (time.time(),)).fetchall()
    for row in rows:
        desired = desired_reaction(row)
        if row['reaction'] == desired:
            continue
        try:
            await set_reaction(adapter, row['channel'], row['message_id'], desired)
        except Exception as exc:
            error_id = f'{board}-reaction-{row["seq"]}'
            response = getattr(exc, 'response', {})
            code = response.get('error', '') if hasattr(response, 'get') else ''
            safe_code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,60}', code) else 'unknown'
            emit('cap_reaction_delivery_failed', error_id=error_id, error_type=type(exc).__name__, code=safe_code)
            with router.connect(board) as conn, conn:
                conn.execute('UPDATE cap_feedback SET failures=failures+1,next_attempt=? WHERE seq=?',
                    (time.time() + min(300, 5 * 2 ** min(row['failures'], 6)), row['seq']))
        else:
            with router.connect(board) as conn, conn:
                conn.execute('UPDATE cap_feedback SET reaction=?,failures=0,next_attempt=0 WHERE seq=?',
                             (desired, row['seq']))
            emit('cap_reaction_delivered', board=board, seq=row['seq'], reaction=desired)


async def send(gateway, channel, thread, text):
    for platform, adapter in gateway.adapters.items():
        if str(getattr(platform, 'value', platform)) == 'slack':
            result = await adapter.send(channel, text, reply_to=thread,
                metadata={'thread_id': thread, 'slack_team_id': WORKSPACE})
            if not getattr(result, 'success', False):
                raise RuntimeError('Slack rejected delivery')
            return
    raise RuntimeError('Slack adapter unavailable')


async def pump(gateway):
    from hermes_cli import kanban_db as kb
    router = Router(kb)
    while True:
        for board in ROUTES.values():
            try:
                with router.connect(board) as conn:
                    pending = conn.execute('SELECT m.seq,m.ack,t.channel,t.thread FROM cap_messages m '
                        'JOIN cap_threads t USING(route_key) WHERE m.delivered=0 ORDER BY m.seq').fetchall()
                for message in pending:
                    try:
                        if message['ack']:
                            await send(gateway, message['channel'], message['thread'], message['ack'])
                    except Exception as exc:
                        emit('cap_ack_delivery_failed', board=board, seq=message['seq'], error_type=type(exc).__name__)
                    else:
                        with router.connect(board) as conn, conn:
                            conn.execute('UPDATE cap_messages SET delivered=1 WHERE seq=?', (message['seq'],))
                await asyncio.to_thread(router.reconcile, board)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                emit('cap_routing_tick_failed', board=board, error_type=type(exc).__name__)
        await asyncio.sleep(5)


async def feedback_pump(gateway):
    from hermes_cli import kanban_db as kb
    router = Router(kb)
    while True:
        for board in ROUTES.values():
            try:
                await update_feedback(gateway, router, board)
            except Exception as exc:
                emit('cap_feedback_tick_failed', board=board, error_type=type(exc).__name__)
        await asyncio.sleep(3)


def start(gateway):
    if os.environ.get('RAILWAY_SERVICE_NAME') == 'Hermes-Cap':
        task = getattr(gateway, '_cap_routing_task', None)
        if task is None or task.done():
            gateway._cap_routing_task = asyncio.create_task(pump(gateway))
        feedback = getattr(gateway, '_cap_feedback_task', None)
        if feedback is None or feedback.done():
            gateway._cap_feedback_task = asyncio.create_task(feedback_pump(gateway))


async def dispatch(event, gateway):
    source = event.source
    if (os.environ.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap'
            or str(getattr(source.platform, 'value', source.platform)) != 'slack'
            or source.chat_id not in ROUTES):
        return False
    # This integration runs before Hermes's ordinary authorization gate.
    if source.scope_id != WORKSPACE or not gateway._is_user_authorized_for_source(source):
        emit('cap_route_unauthorized', channel=source.chat_id)
        return True
    thread = source.thread_id or event.message_id
    try:
        start(gateway)
        from hermes_cli import kanban_db as kb
        await asyncio.to_thread(Router(kb).enqueue, source.chat_id, thread, event.message_id,
                                source.user_id, event.text, event.media_urls,
                                getattr(event, 'channel_context', '') or '')
    except Exception as exc:
        error = uuid.uuid4().hex[:12]
        emit('cap_ingress_failed', error_id=error, channel=source.chat_id, reason=error_hint(exc))
        try:
            await send(gateway, source.chat_id, thread,
                       f'Cap could not register this request. No coding started. {error_hint(exc)}. '
                       f'Error `{error}` in Hermes-Cap logs.')
        except Exception:
            emit('cap_error_delivery_failed', error_id=error)
    return True
