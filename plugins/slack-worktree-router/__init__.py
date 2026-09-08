"""Hermes registration surface for the Slack worktree router."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from typing import Any

from .router import Router, RouterError, authorized_route, log_event, normalize_platform, safe_detail


ROUTER = Router()
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _slack_adapter(gateway: Any) -> Any | None:
    adapters = getattr(gateway, "adapters", {}) if gateway is not None else {}
    for key, adapter in getattr(adapters, "items", lambda: [])():
        if normalize_platform(key) == "slack" or normalize_platform(getattr(adapter, "name", None)) == "slack":
            return adapter
    return None


async def _deliver_slack(
    gateway: Any,
    *,
    channel_id: str,
    thread_ts: str,
    workspace_id: str,
    content: str,
    event_name: str,
    error_id: str | None = None,
) -> bool:
    adapter = _slack_adapter(gateway)
    if adapter is None:
        log_event(logging.ERROR, f"{event_name}_delivery_failed", error_id=error_id,
                  channel_id=channel_id, thread_ts=thread_ts, detail="Slack adapter unavailable")
        return False
    metadata = {"thread_id": thread_ts, "slack_team_id": workspace_id, "team_id": workspace_id}
    try:
        result = adapter.send(channel_id, content, reply_to=thread_ts, metadata=metadata)
        if inspect.isawaitable(result):
            result = await result
        success = bool(getattr(result, "success", True))
        log_event(logging.INFO if success else logging.ERROR,
                  f"{event_name}_delivered" if success else f"{event_name}_delivery_failed",
                  error_id=error_id, channel_id=channel_id, thread_ts=thread_ts,
                  detail=getattr(result, "error", None))
        return success
    except Exception as exc:
        log_event(logging.ERROR, f"{event_name}_delivery_failed", error_id=error_id,
                  channel_id=channel_id, thread_ts=thread_ts, detail=str(exc))
        return False


def _track_task(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _error_message(error_id: str, detail: Any) -> str:
    return (
        ":warning: *Atlas’s coding turn failed.*\n"
        "No fallback model was used.\n"
        f"Error ID: `{error_id}`\n"
        f"Details: {safe_detail(detail, 500)}\n"
        "The same Error ID is in the Hermes-Team and DigitalOcean helper logs."
    )


def _schedule_error(
    gateway: Any,
    *,
    channel_id: str,
    thread_ts: str,
    workspace_id: str,
    error_id: str,
    detail: str,
) -> bool:
    try:
        task = asyncio.get_running_loop().create_task(_deliver_slack(
            gateway, channel_id=channel_id, thread_ts=thread_ts, workspace_id=workspace_id,
            event_name="atlas_error", error_id=error_id, content=_error_message(error_id, detail),
        ))
        _track_task(task)
        return True
    except Exception as exc:
        log_event(logging.ERROR, "atlas_error_delivery_failed", error_id=error_id,
                  channel_id=channel_id, thread_ts=thread_ts, detail=str(exc))
        return False


async def _run_atlas_turn(
    *,
    gateway: Any,
    session_id: str,
    workspace_id: str,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    prompt: str,
) -> None:
    try:
        mapping = await asyncio.to_thread(
            ROUTER.provision, session_id=session_id, workspace_id=workspace_id,
            channel_id=channel_id, thread_ts=thread_ts, user_id=user_id,
        )
        log_event(logging.INFO, "workspace_ready", session_id=mapping.session_id,
                  workspace_id=mapping.workspace_id, channel_id=mapping.channel_id,
                  thread_ts=mapping.thread_ts, github_repo=mapping.github_repo,
                  branch=mapping.branch, base_sha=mapping.base_sha)
        await _deliver_slack(
            gateway, channel_id=channel_id, thread_ts=thread_ts, workspace_id=workspace_id,
            event_name="atlas_started",
            content=(
                ":gear: *Atlas is working in an isolated Codex workspace.*\n"
                f"Repository: `{mapping.github_repo}`\n"
                f"Branch: `{mapping.branch}`\n"
                f"Base: `{mapping.base_branch}` @ `{mapping.base_sha[:12]}`\n"
                "GitHub is the source of truth."
            ),
        )
        result = await asyncio.to_thread(
            ROUTER.run_codex, mapping, f"{ROUTER.prompt(mapping)}\n\nSlack request:\n{prompt}"
        )
        await _deliver_slack(
            gateway, channel_id=channel_id, thread_ts=thread_ts, workspace_id=workspace_id,
            event_name="atlas_final", content=result["final"],
        )
        log_event(logging.INFO, "atlas_turn_succeeded", session_id=mapping.session_id,
                  codex_thread_id=result["codex_thread_id"], resumed=result["resumed"])
    except Exception as exc:
        error_id = getattr(exc, "error_id", uuid.uuid4().hex[:12])
        log_event(logging.ERROR, "atlas_turn_failed", error_id=error_id, session_id=session_id,
                  workspace_id=workspace_id, channel_id=channel_id, thread_ts=thread_ts,
                  user_id=user_id, error_type=type(exc).__name__, error=str(exc))
        await _deliver_slack(
            gateway, channel_id=channel_id, thread_ts=thread_ts, workspace_id=workspace_id,
            event_name="atlas_error", error_id=error_id, content=_error_message(error_id, exc),
        )


def _pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **_: Any,
):
    source = getattr(event, "source", None)
    if source is None or normalize_platform(getattr(source, "platform", None)) != "slack":
        return None
    channel_id = str(getattr(source, "chat_id", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    workspace_id = str(getattr(source, "scope_id", "") or "")
    try:
        config = ROUTER.config()
        if config.routes.get(channel_id) is None:
            return None
        if authorized_route(config, workspace_id, channel_id, user_id) is None:
            log_event(logging.WARNING, "atlas_request_rejected", workspace_id=workspace_id,
                      channel_id=channel_id, user_id=user_id)
            return {"action": "skip", "reason": "atlas-route-unauthorized"}
        if session_store is None:
            raise RouterError("Hermes session store is unavailable")
        entry = session_store.get_or_create_session(source, touch_activity=False)
        thread_ts = str(getattr(source, "thread_id", None) or getattr(event, "message_id", None) or "")
        if not thread_ts:
            raise RouterError("Slack event has no stable thread timestamp")
        prompt = str(getattr(event, "text", "") or "")
        channel_context = str(getattr(event, "channel_context", "") or "").strip()
        if channel_context:
            prompt = f"Slack thread context before this request:\n{channel_context}\n\nCurrent request:\n{prompt}"
        task = asyncio.get_running_loop().create_task(_run_atlas_turn(
            gateway=gateway, session_id=str(entry.session_id), workspace_id=workspace_id,
            channel_id=channel_id, thread_ts=thread_ts, user_id=user_id,
            prompt=prompt,
        ))
        _track_task(task)
        return {"action": "skip", "reason": "atlas-codex-dispatched"}
    except Exception as exc:
        error_id = getattr(exc, "error_id", uuid.uuid4().hex[:12])
        thread_ts = str(getattr(source, "thread_id", None) or getattr(event, "message_id", None) or "")
        log_event(logging.ERROR, "atlas_dispatch_failed", error_id=error_id,
                  workspace_id=workspace_id, channel_id=channel_id, thread_ts=thread_ts,
                  user_id=user_id, error_type=type(exc).__name__, error=str(exc))
        if thread_ts:
            _schedule_error(gateway, channel_id=channel_id, thread_ts=thread_ts,
                            workspace_id=workspace_id, error_id=error_id, detail=str(exc))
        return {"action": "skip", "reason": f"atlas-route-error:{error_id}"}


def _pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any):
    return ROUTER.tool_directive(tool_name, args, session_id)


def _system_prompt(session_info: Any) -> str:
    session_id = str(session_info.get("session_id", "") if session_info else "")
    if not session_id:
        return ""
    try:
        mapping = ROUTER.lookup(session_id)
        return ROUTER.prompt(mapping) if mapping else ""
    except Exception:
        return ""


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_system_prompt_section(
        "slack-worktree-router.workspace", _system_prompt,
        position="after_memory", max_chars=2400,
    )
