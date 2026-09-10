"""One-time adoption of existing work, without rerunning it or changing the PR."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request


def history(channel, thread):
    from dotenv import dotenv_values
    token = os.environ.get('SLACK_BOT_TOKEN') or dotenv_values('/data/.hermes/.env').get('SLACK_BOT_TOKEN')
    if not token:
        raise RuntimeError('Cap Slack token unavailable for context import')
    def call(method, **params):
        request = urllib.request.Request('https://slack.com/api/' + method,
            data=urllib.parse.urlencode(params).encode(), headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Slack history import failed')
        return result
    identity = call('auth.test')
    if identity.get('team_id') != 'TLREWAM8X' or identity.get('user') != 'cap_ai':
        raise RuntimeError('Unexpected Slack identity')
    cursor, messages = '', []
    while True:
        page = call('conversations.replies', channel=channel, ts=thread, limit=100, cursor=cursor)
        messages.extend(page.get('messages', []))
        cursor = page.get('response_metadata', {}).get('next_cursor', '')
        if not cursor:
            return messages


def main():
    if os.environ.get('RAILWAY_SERVICE_NAME') != 'Hermes-Cap':
        raise RuntimeError('Cap only')
    from hermes_cli import kanban_db as kb
    # Works both before deployment (candidate) and in the installed image.
    try:
        from gateway import cap_routing as routing
    except ImportError:
        import routing
    board, channel, thread = 'vw-site', 'C0BV1CCCPS8', '1788976794.031949'
    key = hashlib.sha256(f'{routing.WORKSPACE}/{channel}/{thread}'.encode()).hexdigest()
    path = '/data/cap/repos/vw-site/.worktrees/pr-15'
    branch = 'design/deep-mocha-textures'
    sha = '2262ad1041e782ff608b89bea392b78767cea9df'
    result = subprocess.run(['gh', 'api', 'repos/gldnlab/vw-site/pulls/15'],
                            check=True, capture_output=True, text=True, timeout=30)
    pr = json.loads(result.stdout)
    if pr['head']['sha'] != sha or pr['state'] != 'open' or pr['merged']:
        raise RuntimeError('PR changed; inspect before importing')
    if routing.git(path, 'rev-parse', 'HEAD') != sha or routing.git(path, 'status', '--porcelain'):
        raise RuntimeError('Existing work changed; inspect before importing')
    router = routing.Router(kb)
    with routing.locked(routing.ROOT, board), router.connect(board) as conn:
        existing = conn.execute('SELECT * FROM cap_threads WHERE route_key=?', (key,)).fetchone()
        if existing:
            router.validate(board, existing)
            if kb.get_task(conn, existing['task_id']).status != 'review':
                raise RuntimeError('Partially imported task needs operator review')
            kb.add_notify_sub(conn, task_id=existing['task_id'], platform='slack', chat_id=channel, thread_id=thread,
                user_id='U013H8QQBGT', chat_type='group', notifier_profile='default', delivery_mode='notify',
                delivery_metadata={'slack_team_id': routing.WORKSPACE})
            print(json.dumps({'event': 'cap_existing_work_already_imported', 'task_id': existing['task_id']}))
            return
        messages = history(channel, thread)
        body = ('Adopted existing work; do not reimplement or merge during migration. '
                'This Slack thread explores near-black espresso and alternative surface textures. '
                'PR https://github.com/gldnlab/vw-site/pull/15 is open and draft. '
                f'Existing branch {branch}, verified HEAD {sha}. '
                'Preview https://vw-site-q9v0lsjra-gldnlab.vercel.app (deployment success checked; '
                'visual/accessibility review still requires Derek). GitHub checks passed at import. '
                'Wait for Derek’s next request and exact-commit merge approval. '
                'The prior /tmp/vw-mocha worktree has been preserved at the mapped persistent path.')
        base = routing.git(path, 'merge-base', 'HEAD', 'origin/main')
        # Import an already-finished implementation directly into Review in
        # the same transaction. Never expose it to automatic worker dispatch.
        with kb.write_txn(conn):
            task = kb.create_task(conn, title='Deep mocha and surface textures — existing PR #15', body=body,
                assignee='default', created_by='cap-slack-router', workspace_kind='worktree',
                workspace_path=path, branch_name=branch, initial_status='blocked', board=board,
                idempotency_key='cap-slack:' + key, reasoning_effort='medium')
            conn.execute("UPDATE tasks SET status='review' WHERE id=? AND status='blocked'", (task,))
            kb._append_event(conn, task, 'review_requested', {'implementer': 'default',
                'summary': 'Imported existing PR #15; awaiting Derek’s review', 'imported': True,
                'pr_url': 'https://github.com/gldnlab/vw-site/pull/15', 'head_sha': sha})
            conn.execute("INSERT INTO cap_threads(route_key,channel,thread,task_id,path,branch,base_sha,state) "
                         "VALUES(?,?,?,?,?,?,?,'active')", (key, channel, thread, task, path, branch, base))
        row = conn.execute('SELECT * FROM cap_threads WHERE route_key=?', (key,)).fetchone()
        router.validate(board, row)
        for message in messages:
            text = message.get('text', '')
            if text:
                kb.add_comment(conn, task, 'imported-slack:' + message.get('user', 'bot'),
                               json.dumps({'historical_message': message.get('ts'), 'text': text}))
        kb.add_notify_sub(conn, task_id=task, platform='slack', chat_id=channel, thread_id=thread,
            user_id='U013H8QQBGT', chat_type='group', notifier_profile='default', delivery_mode='notify',
            delivery_metadata={'slack_team_id': routing.WORKSPACE})
        print(json.dumps({'event': 'cap_existing_work_imported', 'task_id': task, 'status': 'review',
                          'branch': branch, 'head_sha': sha, 'worktree': path}))


if __name__ == '__main__':
    main()
