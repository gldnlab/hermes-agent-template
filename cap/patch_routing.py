"""Small, anchor-checked native integration; fail the build on upstream drift."""
from pathlib import Path


def replace_once(source, anchor, replacement):
    if source.count(anchor) != 1 or 'cap_routing' in source:
        raise RuntimeError('Hermes routing integration changed; review before deploying')
    return source.replace(anchor, replacement)


def patch_gateway(source):
    anchor = '        # Fire pre_gateway_dispatch plugin hook for user-originated messages.'
    insertion = '''        # Cap's repo ingress is mandatory, not an optional model/plugin decision.
        if not is_internal and os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
            from gateway.cap_routing import dispatch as cap_dispatch
            if await cap_dispatch(event, self):
                return None

'''
    result = replace_once(source, anchor, insertion + anchor)
    startup = '        logger.info("Starting Hermes Gateway...")'
    if result.count(startup) != 1:
        raise RuntimeError('Hermes gateway startup changed')
    return result.replace(startup, startup + '''
        if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
            from gateway.cap_routing import start as start_cap_routing
            start_cap_routing(self)
''')


def patch_kanban(source):
    anchor = '    prompt = f"work kanban task {task.id}"'
    result = replace_once(source, anchor, anchor + '''
    if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
        from gateway.cap_routing import worker_prompt
        prompt += worker_prompt(task, workspace, board)
''')
    return patch_duplicate_guard(result)


def patch_duplicate_guard(result):
    guard = '    # 3. Completed run within guard window — proof of recent success.'
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
    anchor = '                            _send_res = await adapter.send('
    if source.count(anchor) != 1 or 'full_notification' in source:
        raise RuntimeError('Hermes notification delivery changed')
    return source.replace(anchor, '''                            from gateway.cap_routing import full_notification
                            msg = full_notification(board_slug, sub, ev, msg)
''' + anchor)


if __name__ == '__main__':
    for name, patch in [('gateway/run.py', patch_gateway), ('hermes_cli/kanban_db.py', patch_kanban),
                        ('plugins/platforms/slack/adapter.py', patch_slack_feedback),
                        ('gateway/kanban_watchers.py', patch_notifier)]:
        target = Path('/opt/hermes-agent') / name
        result = patch(target.read_text())
        compile(result, str(target), 'exec')
        target.write_text(result)
