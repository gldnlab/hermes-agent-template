"""Small, anchor-checked native integration; fail the build on upstream drift."""
from pathlib import Path


def replace_once(source, anchor, replacement):
    if source.count(anchor) != 1 or 'cap_routing' in source:
        raise RuntimeError('Hermes routing integration changed; review before deploying')
    return source.replace(anchor, replacement)


def patch_gateway(source):
    # Hermes v2026.9.x moved ingress gating into gateway/run_inbound.py
    # (_hm_admit_event); internal events have already returned by this point.
    anchor = '        event = await self._hm_pre_gateway_dispatch_hook(event, source)'
    insertion = '''        # Cap's repo ingress is mandatory, not an optional model/plugin decision.
        if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
            from gateway.cap_routing import dispatch as cap_dispatch
            if await cap_dispatch(event, self):
                return None
'''
    return replace_once(source, anchor, insertion + anchor)


def patch_startup(source):
    startup = '        logger.info("Starting Hermes Gateway...")'
    return replace_once(source, startup, startup + '''
        if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
            from gateway.cap_routing import start as start_cap_routing
            start_cap_routing(self)
''')


def patch_kanban(source):
    # hermes_cli/kanban_db_dispatch.py: the prompt is the final argv entry
    # (`chat -q <prompt>`) built by _worker_argv inside _default_spawn.
    anchor = '    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))'
    result = replace_once(source, anchor, anchor + '''
    if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
        from gateway.cap_routing import worker_prompt
        if cmd[-2] != "-q":
            raise RuntimeError("Hermes worker argv changed")
        cmd[-1] += worker_prompt(task, workspace, board)
''')
    return patch_duplicate_guard(result)


def patch_duplicate_guard(result):
    guard = '    # 3. Completed run within guard window.'
    if result.count(guard) != 1:
        raise RuntimeError('Hermes respawn guard changed')
    return result.replace(guard, '''    # Cap's durable request is an intentional continuation, including PR refinements.
    # This runs AFTER native rate-limit and authentication guards, never around them.
    if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
        from gateway.cap_routing import explicit_request
        if explicit_request(conn, task_id):
            return None

''' + guard)


def patch_slack_feedback(source):
    if 'from gateway.cap_routing import owns_event' in source:
        raise RuntimeError('Hermes Slack feedback already patched')
    for anchor in [
        '        """Add an in-progress reaction when message processing begins."""',
        '        """Swap the in-progress reaction for a final success/failure reaction."""',
    ]:
        if source.count(anchor) != 1:
            raise RuntimeError('Hermes Slack processing hook changed')
        source = source.replace(anchor, anchor + '''
        from gateway.cap_routing import owns_event
        if owns_event(event):
            if event.message_id:
                self._reacting_message_ids.discard(
                    self._workspace_message_marker(str(event.source.scope_id or ""), event.message_id))
            return  # The durable worker lifecycle owns Cap's reactions.
''')
    return source


def patch_notifier(source):
    # gateway/kanban_watchers_notifier.py: _KanbanNotification._send_event.
    anchor = '        _send_res = None\n        async def send_ping():'
    if source.count(anchor) != 1 or 'full_notification' in source:
        raise RuntimeError('Hermes notification delivery changed')
    return source.replace(anchor, '''        from gateway.cap_routing import full_notification
        msg = full_notification(self.board_slug, sub, ev, msg)
''' + anchor)


if __name__ == '__main__':
    for name, patch in [('gateway/run_inbound.py', patch_gateway),
                        ('gateway/run_startup.py', patch_startup),
                        ('hermes_cli/kanban_db_dispatch.py', patch_kanban),
                        ('plugins/platforms/slack/adapter.py', patch_slack_feedback),
                        ('gateway/kanban_watchers_notifier.py', patch_notifier)]:
        target = Path('/opt/hermes-agent') / name
        result = patch(target.read_text())
        compile(result, str(target), 'exec')
        target.write_text(result)
