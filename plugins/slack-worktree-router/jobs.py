"""Durable DigitalOcean queue. SSH submits/reads; systemd owns execution."""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import time

from router import RouterError, safe_detail


def connect(router):
    conn = router._connect(router.config())
    conn.execute("""CREATE TABLE IF NOT EXISTS atlas_jobs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
        thread_key TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', result TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""")
    return conn


def validate_id(job_id):
    if len(job_id) != 64 or any(c not in '0123456789abcdef' for c in job_id):
        raise RouterError('invalid Atlas job ID')


def submit(router, job_id, payload):
    validate_id(job_id)
    # Check routing before persisting anything. Provisioning happens in the worker.
    config = router.config()
    route = config.routes.get(payload.get('channel_id'))
    if (route is None or route.workspace_id != payload.get('workspace_id')
            or payload.get('user_id') not in route.allowed_users):
        raise RouterError('unauthorized Atlas job route')
    for field in ('message_id', 'thread_ts', 'session_id', 'prompt'):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise RouterError(f'Atlas job missing {field}')
    identity = [payload['workspace_id'], payload['channel_id'], payload['message_id']]
    if hashlib.sha256(json.dumps(identity).encode()).hexdigest() != job_id:
        raise RouterError('Atlas job ID does not match its Slack message')
    encoded = json.dumps(payload, sort_keys=True)
    thread_key = json.dumps([payload['workspace_id'], payload['channel_id'], payload['thread_ts']])
    with connect(router) as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT payload FROM atlas_jobs WHERE job_id=?', (job_id,)).fetchone()
        if existing and existing['payload'] != encoded:
            raise RouterError('Atlas job ID reused with different content')
        conn.execute('INSERT OR IGNORE INTO atlas_jobs '
                     '(job_id, thread_key, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                     (job_id, thread_key, encoded, time.time(), time.time()))
    return status(router, job_id)


def status(router, job_id):
    validate_id(job_id)
    with connect(router) as conn:
        row = conn.execute('SELECT * FROM atlas_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        return {'job_id': job_id, 'state': 'missing'}
    return {'job_id': job_id, 'state': row['state'], 'worker_running': worker_running(router),
            'result': json.loads(row['result']) if row['result'] else None}


def worker_running(router):
    path = router.config().state_db.parent / 'worker.lock'
    if not path.exists():
        return False
    with path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def finish(router, job_id, state, result):
    with connect(router) as conn:
        conn.execute('UPDATE atlas_jobs SET state=?, result=?, updated_at=? WHERE job_id=?',
                     (state, json.dumps(result), time.time(), job_id))


def claim(router):
    with connect(router) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""SELECT j.* FROM atlas_jobs j WHERE j.state='queued'
            AND NOT EXISTS (SELECT 1 FROM atlas_jobs earlier
                WHERE earlier.thread_key=j.thread_key AND earlier.seq<j.seq
                AND earlier.state IN ('queued','running'))
            ORDER BY j.seq LIMIT 1""").fetchone()
        if row:
            conn.execute("UPDATE atlas_jobs SET state='running', updated_at=? WHERE job_id=?",
                         (time.time(), row['job_id']))
    return dict(row) if row else None


def execute(router, row):
    from remote_helper import _run_codex, _log
    job_id = row['job_id']
    payload = json.loads(row['payload'])
    try:
        mapping = router.provision(**{k: payload[k] for k in
            ('session_id', 'workspace_id', 'channel_id', 'thread_ts', 'user_id')})
        result = _run_codex(router, mapping,
            f"{router.prompt(mapping)}\n\nSlack request:\n{payload['prompt']}", job_id=job_id)
        result['mapping'] = mapping.to_dict()
        finish(router, job_id, 'succeeded', result)
        _log('job_succeeded', job_id=job_id)
    except Exception as exc:
        finish(router, job_id, 'failed', {'error_id': job_id,
            'error': safe_detail(exc, 1000)})
        _log('job_failed', job_id=job_id, error=safe_detail(exc))


def recover_interrupted(router):
    """Never replay an uncertain coding turn. Recover a saved final when possible."""
    from remote_helper import _parse_codex_jsonl, _save_codex_session, _log
    with connect(router) as conn:
        rows = conn.execute("SELECT * FROM atlas_jobs WHERE state='running'").fetchall()
    for row in rows:
        job_id = row['job_id']
        path = router.config().state_db.parent / 'job-events' / f'{job_id}.jsonl'
        try:
            events = path.read_text()
            parsed = [json.loads(line) for line in events.splitlines() if line.strip()]
            payload = json.loads(row['payload'])
            mapping = router.lookup(payload['session_id'])
            if mapping is None:
                raise RouterError('mapping missing after worker restart')
            # Preserve a new session even if its first turn was interrupted.
            for event in parsed:
                if event.get('type') == 'thread.started':
                    _save_codex_session(router, mapping, event['thread_id'])
                    break
            if not any(e.get('type') == 'turn.completed' for e in parsed):
                raise RouterError('turn completion not recorded')
            thread_id, final = _parse_codex_jsonl(events)
            finish(router, job_id, 'succeeded', {'codex_thread_id': thread_id,
                'final': final, 'resumed': True, 'mapping': mapping.to_dict()})
            _log('job_result_recovered', job_id=job_id)
        except Exception as exc:
            finish(router, job_id, 'interrupted', {'error_id': job_id,
                'error': 'The DigitalOcean worker restarted before recording completion. '
                         'Work may have been saved. Atlas has not replayed this request; '
                         'ask it to inspect the existing work before continuing.'})
            _log('job_interrupted', job_id=job_id, error=safe_detail(exc))


def worker(router):
    config = router.config()
    config.state_db.parent.mkdir(parents=True, exist_ok=True)
    with (config.state_db.parent / 'worker.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover_interrupted(router)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pending = set()
            while True:
                for future in list(pending):
                    if future.done():
                        future.result()
                        pending.remove(future)
                if len(pending) < 4:
                    row = claim(router)
                    if row:
                        pending.add(pool.submit(execute, router, row))
                        continue
                time.sleep(1)
