"""Persistent Railway inbox/outbox and restart-safe Slack result delivery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid

from .router import RouterError, log_event, safe_detail


class Delivery:
    def __init__(self, router, client_factory=None):
        self.router = router
        self.client_factory = client_factory or self.slack_client
        self.thread = None

    def connect(self):
        root = self.state_dir()
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(root / 'atlas-delivery.sqlite3', timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS deliveries (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL, slack_ts TEXT, last_content TEXT,
            delivered INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            created_at REAL NOT NULL
        )''')
        return conn

    def state_dir(self):
        if self.router.config().backend == 'ssh':
            # The configured state_db names the REMOTE helper DB. It is not
            # the Railway volume and must never be used for the local inbox.
            from hermes_cli.config import get_hermes_home
            return get_hermes_home() / 'atlas'
        return self.router.config().state_db.parent  # local test harness

    def enqueue(self, payload):
        identity = [payload['workspace_id'], payload['channel_id'], payload['message_id']]
        job_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if len(json.dumps(payload).encode()) > 48 * 1024:
            raise RouterError('Slack request exceeds the 48 KiB Atlas limit')
        with self.connect() as conn:
            # Slack redelivery must retain the original payload/session, even
            # if Hermes has reset its own session since first receipt.
            conn.execute('INSERT OR IGNORE INTO deliveries (job_id,payload,created_at) VALUES (?,?,?)',
                         (job_id, json.dumps(payload), time.time()))
        log_event(logging.INFO, 'atlas_request_saved', job_id=job_id)
        return job_id

    @staticmethod
    def slack_client():
        from gateway.config import load_gateway_config, Platform
        from slack_sdk import WebClient
        platform = load_gateway_config().platforms.get(Platform.SLACK)
        if platform is None or not platform.enabled or not platform.token:
            raise RouterError('Atlas default-profile Slack connection is unavailable')
        return WebClient(token=platform.token, timeout=15)

    def publish(self, row, payload, content):
        client = self.client_factory()
        # Check workspace identity before using a token loaded for this profile.
        auth = client.auth_test()
        if auth['team_id'] != payload['workspace_id']:
            raise RouterError('Slack delivery token belongs to a different workspace')
        ts = row['slack_ts']
        if not ts:
            # Resolve the post-success/database-write crash window by searching
            # for our stable job marker. Never assume a lost HTTP reply failed.
            cursor = None
            while True:
                page = client.conversations_replies(channel=payload['channel_id'],
                    ts=payload['thread_ts'], limit=100, cursor=cursor,
                    include_all_metadata=True)
                for message in page.get('messages', []):
                    metadata = message.get('metadata') or {}
                    if (message.get('user') == auth['user_id']
                            and metadata.get('event_type') == 'atlas_job'
                            and (metadata.get('event_payload') or {}).get('job_id') == row['job_id']):
                        ts = message['ts']
                        break
                cursor = (page.get('response_metadata') or {}).get('next_cursor')
                if ts or not cursor:
                    break
        # Slack uses its own link syntax; Codex returns ordinary Markdown.
        content = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<\2|\1>', content)
        if len(content) > 35000:
            raise RouterError('Atlas reply exceeds Slack delivery limit; full result retained on DigitalOcean')
        metadata = {'event_type': 'atlas_job', 'event_payload': {'job_id': row['job_id']}}
        if ts:
            result = client.chat_update(channel=payload['channel_id'], ts=ts,
                text=content, metadata=metadata)
        else:
            result = client.chat_postMessage(channel=payload['channel_id'],
                thread_ts=payload['thread_ts'], text=content, metadata=metadata,
                client_msg_id=str(uuid.UUID(row['job_id'][:32])),
                unfurl_links=False, unfurl_media=False)
            ts = result['ts']
        if not result.get('ok'):
            raise RouterError(f"Slack delivery rejected: {result.get('error', 'unknown error')}")
        with self.connect() as conn:
            conn.execute('UPDATE deliveries SET slack_ts=?,last_content=? WHERE job_id=?',
                         (ts, content, row['job_id']))
        log_event(logging.INFO, 'atlas_reply_saved_in_slack', job_id=row['job_id'], slack_ts=ts)

    def tick(self):
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM deliveries WHERE delivered=0 ORDER BY seq').fetchall()
        blocked_threads = set()
        for row in rows:
            payload = json.loads(row['payload'])
            thread_key = (payload['workspace_id'], payload['channel_id'], payload['thread_ts'])
            if thread_key in blocked_threads:
                continue
            if row['retry_at'] > time.time():
                blocked_threads.add(thread_key)
                continue
            job_id = row['job_id']
            try:
                config = self.router.config()
                remote = self.router._remote_request(config, 'job_submit', job_id=job_id, payload=payload)
                state = remote['state']
                terminal = state in {'succeeded', 'failed', 'interrupted'}
                if state == 'succeeded':
                    content = remote['result']['final']
                elif terminal:
                    content = (f":warning: Atlas’s job {state}.\n"
                               f"{remote['result']['error']}\nJob ID: `{job_id}`")
                elif state == 'running':
                    content = (f"Atlas is working · `{config.codex_model}` · "
                               f"`{config.codex_reasoning_effort}` reasoning.\n"
                               'This job and its reply will survive a Hermes restart.')
                else:
                    content = 'Atlas has saved this request and queued it for the coding worker.'
                    if remote.get('worker_running') is False:
                        content += '\n:warning: The coding worker is offline. The saved job will run when it recovers.'
                    elif time.time() - row['created_at'] > 120:
                        content += '\nStill waiting for a worker or an earlier request in this thread.'
                normalized = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<\2|\1>', content)
                if normalized != row['last_content']:
                    self.publish(row, payload, content)
                with self.connect() as conn:
                    conn.execute('UPDATE deliveries SET delivered=?,failures=0,last_error=NULL,retry_at=0 WHERE job_id=?',
                                 (int(terminal), job_id))
                if terminal:
                    log_event(logging.INFO, 'atlas_result_delivered', job_id=job_id, state=state)
            except Exception as exc:
                blocked_threads.add(thread_key)
                failures = row['failures'] + 1
                detail = safe_detail(exc, 1000)
                with self.connect() as conn:
                    conn.execute('UPDATE deliveries SET failures=?,last_error=?,retry_at=? WHERE job_id=?',
                                 (failures, detail, time.time() + min(60, 2 ** min(failures, 6)), job_id))
                log_event(logging.ERROR, 'atlas_delivery_retry', job_id=job_id,
                          attempt=failures, error=detail)
                # This describes transport uncertainty, never a coding failure.
                if failures == 2:
                    try:
                        self.publish(row, payload,
                            ':warning: Atlas’s connection or reply delivery was interrupted. '
                            'Your request is saved; I’m retrying automatically. '
                            f'Job ID: `{job_id}`')
                    except Exception:
                        pass  # the durable retry/error record above remains authoritative

    def run(self):
        while True:
            try:
                root = self.state_dir()
                root.mkdir(parents=True, exist_ok=True)
                with (root / 'atlas-delivery.lock').open('a+') as lock:
                    # Only one relay owns posting, including overlapping gateways.
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    log_event(logging.INFO, 'atlas_delivery_recovery_started')
                    while True:
                        self.tick()
                        time.sleep(3)
            except Exception as exc:
                log_event(logging.ERROR, 'atlas_delivery_recovery_retry', error=safe_detail(exc))
                time.sleep(5)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run, name='atlas-delivery', daemon=True)
            self.thread.start()
