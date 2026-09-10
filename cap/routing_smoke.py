"""Run with installed Hermes; isolated temporary boards/repos, no Slack sends."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import inspect
import sys
from types import SimpleNamespace


def main():
    root = Path(tempfile.mkdtemp(prefix='cap-routing-smoke-'))
    os.environ['HERMES_KANBAN_HOME'] = str(root / 'hermes')
    from hermes_cli import kanban_db as kb
    spec = importlib.util.spec_from_file_location('candidate', Path(__file__).with_name('routing.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root / 'cap'
    router = mod.Router(kb, mod.ROOT)
    def command(*args):
        subprocess.run(args, check=True, capture_output=True)
    for board in ('vw-site', 'vw-hq'):
        repo = mod.ROOT / 'repos' / board
        repo.mkdir(parents=True)
        command('git', 'init', '-b', 'main', str(repo))
        command('git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '--allow-empty', '-m', 'fixture')
        command('git', '-C', str(repo), 'remote', 'add', 'origin', f'https://github.com/gldnlab/{board}.git')
        kb.create_board(board, default_workdir=str(repo))
    actual_git = mod.git
    def offline_git(path, *args):
        if args[0] == 'fetch':
            return ''
        if args == ('rev-parse', 'FETCH_HEAD^{commit}'):
            return actual_git(path, 'rev-parse', 'HEAD')
        return actual_git(path, *args)
    mod.git = offline_git
    channel, ts = 'C0BV1CCCPS8', '1788976794.031949'
    router.enqueue(channel, ts, ts, 'U013H8QQBGT', 'No-op fixture')
    router.enqueue(channel, ts, ts, 'U013H8QQBGT', 'No-op fixture')
    with router.connect('vw-site') as conn:
        kb.recompute_ready(conn)
        task_id = conn.execute('SELECT task_id FROM cap_threads').fetchone()[0]
        assert kb.get_task(conn, task_id).status == 'blocked', 'Dispatcher must not outrun provisioning'
    router.reconcile('vw-site')
    with router.connect('vw-site') as conn:
        rows = conn.execute('SELECT * FROM cap_threads').fetchall()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row['state'] == 'active', dict(row)
        task = kb.get_task(conn, row['task_id'])
        assert task.status == 'ready', task.status
        assert len(kb.list_comments(conn, task.id)) == 1
        assert len(kb.list_notify_subs(conn)) == 1
        task = kb.claim_task(conn, task.id)
        assert task and task.current_run_id
    # Uses real Git branch/common-dir validation and native API state transitions.
    assert 'No-op fixture' in mod.worker_prompt(task, row['path'], 'vw-site')
    router.enqueue(channel, ts, '1788976795.031949', 'U013H8QQBGT', 'Follow-up fixture')
    with router.connect('vw-site') as conn:
        assert kb.request_review(conn, task.id, summary='Fixture ready for review', force=True)
        first_run = task.current_run_id
        assert conn.execute('SELECT run_id FROM cap_feedback WHERE seq=1').fetchone()[0] == first_run
        assert conn.execute('SELECT run_id FROM cap_feedback WHERE seq=2').fetchone()[0] is None
    router.reconcile('vw-site')
    with router.connect('vw-site') as conn:
        task = kb.get_task(conn, task.id)
        assert task.status == 'ready', task.status
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        task = kb.claim_task(conn, task.id)
    assert 'Follow-up fixture' in mod.worker_prompt(task, row['path'], 'vw-site')
    with router.connect('vw-site') as conn:
        full = 'Complete answer\n' + 'Detailed finding. ' * 400 + '\nhttps://preview.example.test/\nFinal caveat'
        assert kb.request_review(conn, task.id, summary=full, force=True)
        event = [e for e in kb.list_events(conn, task.id) if e.kind == 'review_requested'][-1]
        assert conn.execute('SELECT run_id FROM cap_feedback WHERE seq=1').fetchone()[0] == first_run
        assert conn.execute('SELECT run_id FROM cap_feedback WHERE seq=2').fetchone()[0] == task.current_run_id
    sub = dict(platform='slack', task_id=task.id, chat_id=channel, thread_id=ts)
    assert full in mod.full_notification('vw-site', sub, event, 'truncated')
    # Return the task to ready for the native duplicate-PR guard checks below.
    with router.connect('vw-site') as conn:
        kb.reopen_review_task(conn, task.id)
    router.enqueue('C0BUTAUA88M', ts, ts, 'U013H8QQBGT', 'Other repository')
    router.reconcile('vw-hq')
    with router.connect('vw-hq') as conn:
        hq = conn.execute('SELECT * FROM cap_threads').fetchone()
        assert hq['state'] == 'active' and '/vw-hq/' in hq['path']
        assert hq['path'] != row['path']
    # Source patches must still fit this installed Hermes version.
    patch_spec = importlib.util.spec_from_file_location('patch', Path(__file__).with_name('patch_routing.py'))
    patch = importlib.util.module_from_spec(patch_spec)
    patch_spec.loader.exec_module(patch)
    # Real native PR guard, patched in THIS isolated test process only.
    sys.modules['gateway.cap_routing'] = mod
    guard_source = inspect.getsource(kb.check_respawn_guard)
    if 'from gateway.cap_routing import explicit_request' not in guard_source:
        exec(compile(patch.patch_duplicate_guard(guard_source), '<guard>', 'exec'), kb.__dict__)
    with router.connect('vw-site') as conn:
        kb.add_comment(conn, task.id, 'test', 'Existing PR https://github.com/gldnlab/vw-site/pull/15')
        assert kb.check_respawn_guard(conn, task.id) is None
        os.environ['RAILWAY_SERVICE_NAME'] = 'Hermes-Team'
        assert kb.check_respawn_guard(conn, task.id) == 'active_pr'
        os.environ['RAILWAY_SERVICE_NAME'] = 'Hermes-Cap'
        with conn:
            conn.execute('UPDATE tasks SET last_failure_error=? WHERE id=?', ('unauthorized', task.id))
        assert kb.check_respawn_guard(conn, task.id) == 'blocker_auth'
    for name, fn in [('gateway/run.py', patch.patch_gateway), ('hermes_cli/kanban_db.py', patch.patch_kanban)]:
        source = (Path('/opt/hermes-agent') / name).read_text()
        if 'from gateway.cap_routing import' not in source:
            compile(fn(source), name, 'exec')
    for name, fn in [('plugins/platforms/slack/adapter.py', patch.patch_slack_feedback),
                     ('gateway/kanban_watchers.py', patch.patch_notifier)]:
        source = (Path('/opt/hermes-agent') / name).read_text()
        if 'from gateway.cap_routing import' not in source:
            compile(fn(source), name, 'exec')
    print('PASS: per-message native run binding, queued follow-up, full result including final caveat, Slack hook/notifier patches')
    print('PASS: native create/dedup, provisioning hold, Git worktree, boards, subscription, follow-up, PR guard scope, auth guard, patches')


if __name__ == '__main__':
    main()
