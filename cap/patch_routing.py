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
    return replace_once(source, anchor, anchor + '''
    if os.environ.get("RAILWAY_SERVICE_NAME") == "Hermes-Cap":
        from gateway.cap_routing import worker_prompt
        prompt += worker_prompt(task, workspace, board)
''')


if __name__ == '__main__':
    for name, patch in [('gateway/run.py', patch_gateway), ('hermes_cli/kanban_db.py', patch_kanban)]:
        target = Path('/opt/hermes-agent') / name
        result = patch(target.read_text())
        compile(result, str(target), 'exec')
        target.write_text(result)
