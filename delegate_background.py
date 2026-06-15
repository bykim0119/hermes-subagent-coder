"""Background coder delegation — spawns a Codex CLI coder child detached.

Moved out of ``tools/delegate_tool.py`` so the stock file stays diff-free and
``subagent_coder`` is installable as a standalone ``~/.hermes/plugins/`` unit.

The coder child runs the STOCK ``delegate_task`` (no coder-specific params).
Provider / api_mode / subagent_id overrides are injected at runtime by the
wraps installed in ``subagent_coder.register(ctx)`` via the ``_coder_child_ctx``
ContextVar set inside ``_spawn_detached_coder._runner`` below. Because the coder
spawn is a single-task delegation it runs on the ``_runner`` thread inline (no
ThreadPoolExecutor boundary), so the ContextVar propagates cleanly.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

# Stock symbols stay in tools.delegate_tool; this plugin module loads at
# register(ctx) time, long after tools.delegate_tool is fully imported, so a
# module-top import is safe (no cycle). ``interrupt_subagent`` is looked up in
# THIS module's namespace by ``cancel_coder_run`` — tests patch it here.
from tools.delegate_tool import (
    registry,
    check_delegate_requirements,
    interrupt_subagent,
)

logger = logging.getLogger(__name__)


# Runtime channel for the coder child overrides. Set by ``_spawn_detached_coder``
# right before calling the stock ``delegate_task`` and read by the wraps around
# ``_build_child_agent`` / ``_build_child_progress_callback`` (installed in
# ``subagent_coder.register(ctx)``). ``None`` outside a coder spawn.
_coder_child_ctx: ContextVar[Optional[dict]] = ContextVar(
    "coder_child_ctx", default=None
)

# Carries the running AIAgent into the registry handler on the SEQUENTIAL tool
# dispatch path. ``registry.dispatch`` never forwards parent_agent, and the
# sequential loop (``_execute_tool_calls_sequential``) routes unknown registry
# tools straight to ``handle_function_call`` — so the wrap installed in
# ``subagent_coder.register(ctx)`` sets this ContextVar to ``self`` around the
# loop and the handler below reads it as a fallback. The sequential loop runs
# on the agent's own thread (no ThreadPoolExecutor), so the ContextVar
# propagates. The concurrent path injects parent_agent directly via the
# ``_invoke_tool`` wrap instead (ContextVars don't cross worker threads).
_dispatch_parent_agent: ContextVar[Optional[Any]] = ContextVar(
    "dispatch_parent_agent", default=None
)


# ---------------------------------------------------------------------------
# Background variant — spawns coder child detached, returns immediately
# ---------------------------------------------------------------------------

_CODER_RUN_REGISTRY: Dict[str, Dict[str, Any]] = {}
_CODER_RUN_LOCK = threading.Lock()

_LOG_MAXLEN = 200  # bounded coder NDJSON event tail per run


def _register_coder_run(coder_run_id: str, parent_task_id: str, goal: str) -> None:
    with _CODER_RUN_LOCK:
        _CODER_RUN_REGISTRY[coder_run_id] = {
            "parent_task_id": parent_task_id,
            "goal": goal,
            "started_at": time.time(),
            "status": "running",
            "log": deque(maxlen=_LOG_MAXLEN),
        }


def get_coder_run(coder_run_id: str) -> Optional[Dict[str, Any]]:
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        return dict(rec) if rec else None


def record_main_routing(coder_run_id: str, source: Any, loop: Any) -> None:
    """메인 세션 라우팅 메타데이터 + 이벤트 루프를 런 레코드에 저장.

    ``main_source`` 존재가 런을 *오케스트레이션 대상*으로 표시하는 단일 게이트다
    (agent의 delegate_task_background 경로에서만 기록; /code 슬래시는 기록 안 함).
    오케스트레이션 런만 coder_status에 보이고, cancel_coder로 취소되며, 완료 시
    메인을 깨운다.
    """
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is not None:
            rec["main_source"] = source
            rec["main_loop"] = loop


def list_orchestration_runs() -> List[Dict[str, Any]]:
    """오케스트레이션 런 전체의 요약 리스트(라우팅 없는 /code 런은 제외)."""
    with _CODER_RUN_LOCK:
        return [
            {
                "coder_run_id": cid,
                "goal": rec.get("goal"),
                "status": rec.get("status"),
                "started_at": rec.get("started_at"),
            }
            for cid, rec in _CODER_RUN_REGISTRY.items()
            if rec.get("main_source") is not None
        ]


def get_orchestration_run(
    coder_run_id: str, include: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """단일 오케스트레이션 런 상세. 라우팅 없는 런이면 None.

    ``include``에 "result"가 있으면 result/error, "log"가 있으면 log 전체를 포함.
    """
    wanted = set(include or [])
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is None or rec.get("main_source") is None:
            return None
        out: Dict[str, Any] = {
            "coder_run_id": coder_run_id,
            "goal": rec.get("goal"),
            "status": rec.get("status"),
            "started_at": rec.get("started_at"),
            "parent_task_id": rec.get("parent_task_id"),
        }
        if "result" in wanted:
            if rec.get("result") is not None:
                out["result"] = rec.get("result")
            if rec.get("error") is not None:
                out["error"] = rec.get("error")
        if "log" in wanted:
            out["log"] = list(rec.get("log") or [])
        return out


def claim_completion_notify(coder_run_id: str) -> Optional[Dict[str, Any]]:
    """완료 알림 1회 권한을 원자적으로 claim.

    이 호출이 claim에 성공하면(오케스트레이션 런 + 미알림) ``notified`` 플래그를
    세팅하고 스냅샷 dict를 반환한다 → 동시/중복 완료가 정확히 1회만 주입되도록 보장.
    그 외(라우팅 없음 / 이미 알림)는 None.
    """
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is None or rec.get("main_source") is None:
            return None
        if rec.get("notified"):
            return None
        rec["notified"] = True
        return {
            "goal": rec.get("goal"),
            "status": rec.get("status"),
            "result": rec.get("result"),
            "error": rec.get("error"),
            "source": rec.get("main_source"),
            "loop": rec.get("main_loop"),
            "log": list(rec.get("log") or []),
        }


# Bang-prefixed control tokens. A bare ``cancel`` is rejected by
# ``is_cancel_command`` because such a word can legitimately appear in a
# follow-up instruction; the prefix marks an explicit gateway command.
_CODER_CANCEL_COMMANDS = frozenset({"!cancel", "!stop"})


def is_cancel_command(text: Optional[str]) -> bool:
    """True iff ``text`` (after strip+lower) is a recognized cancel command.

    Used by the Discord adapter's thread message router to short-circuit
    follow-up forwarding when the user wants to terminate the run.
    """
    if not text:
        return False
    return text.strip().lower() in _CODER_CANCEL_COMMANDS


def _attach_coder_client(coder_run_id: str, client: Any) -> None:
    """Attach an active CodexExecClient so ``cancel_coder_run`` can reach it."""
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is not None:
            rec["client"] = client


def cancel_coder_run(coder_run_id: str) -> bool:
    """Cancel an active coder run. Returns True if cancellation took effect."""
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
    if rec is None:
        return False
    client = rec.get("client")
    proc_killed = False
    if client is not None:
        try:
            proc_killed = bool(client.terminate())
        except Exception:
            logger.debug("cancel_coder_run: client.terminate failed", exc_info=True)
    agent_interrupted = interrupt_subagent(coder_run_id)
    if proc_killed or agent_interrupted:
        with _CODER_RUN_LOCK:
            rec2 = _CODER_RUN_REGISTRY.get(coder_run_id)
            if rec2 is not None and rec2.get("status") == "running":
                rec2["status"] = "cancelled"
        return True
    return False


def _build_coder_progress_sink(coder_run_id: str):
    """Sink installed into CodexExecFacade so each NDJSON event is relayed
    to every registered platform adapter via the coder event bus.

    Previously the sink hopped through ``parent_agent.tool_progress_callback``,
    but that callback only existed inside a parent-agent turn — follow-up
    coder runs (spawned outside any parent turn) had no way to reach the
    adapter. The bus removes the parent_agent dependency entirely.

    Also captures the Codex CLI session UUID from the first ``thread.started``
    event so subsequent follow-up turns can use ``codex exec resume <uuid>``
    to re-enter the same conversation context.
    """
    def _sink(event) -> None:
        try:
            if event.event == "thread.started":
                from .coder_sessions import get_global_sessions
                tid = (event.data or {}).get("thread_id")
                sessions = get_global_sessions()
                if tid and sessions is not None:
                    sessions.set_codex_session_id(coder_run_id, tid)
            try:
                with _CODER_RUN_LOCK:
                    rec = _CODER_RUN_REGISTRY.get(coder_run_id)
                    if rec is not None and rec.get("log") is not None:
                        rec["log"].append({"event": event.event, "data": event.data})
            except Exception:
                logger.debug("coder log capture failed", exc_info=True)
            from . import coder_event_bus
            payload = {"event": event.event, "data": event.data}
            coder_event_bus.dispatch(coder_run_id, payload)
        except Exception:
            logger.debug("coder progress sink relay failed", exc_info=True)

    return _sink


def _spawn_detached_coder(
    parent_agent,
    goal: str,
    context: str,
    coder_run_id: str,
    provider: str = "codex-exec",
) -> str:
    """Run the coder child in a background daemon thread.

    Returns immediately with the coder_run_id. The child runs the STOCK
    ``delegate_task``; the ``_coder_child_ctx`` ContextVar (set here) drives the
    register(ctx) wraps that inject ``override_provider``/``override_api_mode``
    and pin ``child._subagent_id`` to ``coder_run_id`` so gateway can route its
    progress events to the matching Discord thread.
    """
    sink = _build_coder_progress_sink(coder_run_id)

    def _runner() -> None:
        # Imported lazily — codex_exec_client lives in this package and the
        # stock delegate_task import would create a cycle if pulled in at top.
        from .codex_exec_client import register_coder_sink, unregister_coder_sink
        from tools.delegate_tool import delegate_task

        register_coder_sink(coder_run_id, sink)
        token = _coder_child_ctx.set(
            {
                "subagent_id": coder_run_id,
                "provider": provider,
                "api_mode": "chat_completions",
            }
        )
        try:
            result = delegate_task(
                parent_agent=parent_agent,
                goal=goal,
                context=context,
                tasks=None,
                toolsets=["terminal", "file"],
                role="leaf",
            )
            with _CODER_RUN_LOCK:
                rec = _CODER_RUN_REGISTRY.get(coder_run_id)
                if rec is not None and rec.get("status") != "cancelled":
                    rec["status"] = "completed"
                    rec["result"] = result
        except Exception as exc:
            logger.exception("Coder run %s failed: %s", coder_run_id, exc)
            with _CODER_RUN_LOCK:
                rec = _CODER_RUN_REGISTRY.get(coder_run_id)
                if rec is not None and rec.get("status") != "cancelled":
                    rec["status"] = "failed"
                    rec["error"] = str(exc)
        finally:
            _coder_child_ctx.reset(token)
            unregister_coder_sink(coder_run_id)
            try:
                from . import coder_orchestration
                coder_orchestration.notify_main_on_completion(coder_run_id)
            except Exception:
                logger.debug("completion notify failed", exc_info=True)

    thread = threading.Thread(
        target=_runner, name=f"coder-{coder_run_id}", daemon=True
    )
    thread.start()
    return coder_run_id


_RESOLVE_SENTINEL = object()


def _resolve_codex_command_and_args() -> tuple[str, list[str]]:
    """Resolve (command, base_args) for codex CLI invocation.

    Priority for each field is:
      env > delegation.coder.<key> in config > auth resolver creds > hardcoded default.

    The auth resolver (``hermes_cli.auth``) provides the canonical
    ``codex`` binary path discovered from provider config plus a default
    args set. Operator-supplied env or config values must be able to
    override those, otherwise ``delegation.coder.args`` is meaningless
    on hosts where the auth resolver succeeds with its own args.
    """
    from .coder_config import coder_setting

    explicit_command = coder_setting(
        "command",
        env_var="HERMES_CODER_COMMAND",
        default=_RESOLVE_SENTINEL,
    )
    explicit_args = coder_setting(
        "args",
        env_var="HERMES_CODER_ARGS",
        default=_RESOLVE_SENTINEL,
    )

    auth_command: Optional[str] = None
    auth_args: Optional[list[str]] = None
    if explicit_command is _RESOLVE_SENTINEL or explicit_args is _RESOLVE_SENTINEL:
        try:
            from hermes_cli.auth import resolve_external_process_provider_credentials
            creds = resolve_external_process_provider_credentials("codex-exec")
            auth_command = creds.get("command") or None
            auth_args = list(creds.get("args") or []) or None
        except Exception:
            pass

    command = explicit_command if explicit_command is not _RESOLVE_SENTINEL else (auth_command or "codex")

    if explicit_args is not _RESOLVE_SENTINEL:
        raw_args = explicit_args
        base_args = raw_args.split() if isinstance(raw_args, str) else list(raw_args)
    elif auth_args:
        base_args = list(auth_args)
    else:
        base_args = [
            "exec", "--json", "--skip-git-repo-check",
            "--sandbox", "workspace-write",
        ]
    return command, base_args


def _spawn_codex_coder(
    coder_run_id: str,
    text: str,
    *,
    resume_session_id: Optional[str] = None,
) -> None:
    """Spawn a codex coder process in a daemon thread.

    Two modes:
      * ``resume_session_id=None`` — fresh ``codex exec`` invocation. Used by
        the ``/code`` slash command which starts a brand-new coder thread
        without going through the LLM-driven ``delegate_task_background``.
      * ``resume_session_id="<UUID>"`` — ``codex exec resume <UUID>`` to
        re-enter an existing conversation. Used for Discord thread follow-up
        messages. The UUID was captured from the original spawn's
        ``thread.started`` event.

    Both modes share:
      * Gateway-level sink (``_build_coder_progress_sink``) — no parent_agent
        dependency. Events flow through ``coder_event_bus``.
      * Workspace = ``os.getcwd()`` (gateway service cwd) — matches the
        CodexExecFacade default used by ``delegate_task_background``.

    Resume-only sanitization: codex's ``resume`` subcommand rejects some
    value-pair options that ``exec`` accepts (``--sandbox <mode>``,
    ``--profile``). The first-spawn args include those, so we translate
    each ``--sandbox X`` into the resume-compatible equivalent (e.g.
    ``danger-full-access`` → ``--dangerously-bypass-approvals-and-sandbox``)
    and drop ``--profile`` (parent already used it to seed config).
    Without translation resume falls back to default ``workspace-write``,
    which crashes bwrap loopback on this VM.
    """
    import asyncio as _asyncio
    from .codex_exec_client import CodexExecClient

    sink = _build_coder_progress_sink(coder_run_id)
    command, base_args = _resolve_codex_command_and_args()

    if resume_session_id:
        _SANDBOX_RESUME_EQUIV = {
            "danger-full-access": ["--dangerously-bypass-approvals-and-sandbox"],
            "workspace-write": ["--full-auto"],
            # read-only is the codex default; explicit equivalent isn't needed.
            "read-only": [],
        }
        cleaned = []
        i = 0
        while i < len(base_args):
            a = base_args[i]
            if a in ("--sandbox", "-s"):
                mode = base_args[i + 1] if i + 1 < len(base_args) else ""
                cleaned.extend(_SANDBOX_RESUME_EQUIV.get(mode, []))
                i += 2
                continue
            if a in ("--profile", "-p"):
                i += 2
                continue
            cleaned.append(a)
            i += 1

        # Insert "resume <UUID>" right after "exec" so the final argv is:
        #   codex exec resume <UUID> --json --skip-git-repo-check <prompt>
        extra_args = list(cleaned)
        if "exec" in extra_args:
            i = extra_args.index("exec")
            extra_args[i + 1:i + 1] = ["resume", resume_session_id]
        else:
            extra_args = ["exec", "resume", resume_session_id, *extra_args]
    else:
        # Fresh spawn — use base_args verbatim. ``exec`` accepts ``--sandbox``
        # so no translation needed.
        extra_args = list(base_args)
        if "exec" not in extra_args:
            extra_args = ["exec", *extra_args]

    client = CodexExecClient(command=command, extra_args=extra_args)
    _attach_coder_client(coder_run_id, client)
    workspace = os.getcwd()
    kind = "followup" if resume_session_id else "fresh"

    def _runner() -> None:
        async def _consume() -> None:
            async for event in client.run(goal=text, workspace=workspace):
                try:
                    sink(event)
                except Exception:
                    logger.debug("coder sink call failed", exc_info=True)

        try:
            _asyncio.run(_consume())
        except Exception as exc:
            logger.exception("Coder %s run %s failed: %s", kind, coder_run_id, exc)

    th = threading.Thread(
        target=_runner,
        name=f"coder-{kind}-{coder_run_id}",
        daemon=True,
    )
    th.start()


def _spawn_followup_coder(
    coder_run_id: str,
    codex_session_id: str,
    text: str,
) -> None:
    """Back-compat wrapper — Discord adapter calls this from
    ``_handle_coder_followup``. Forwards to ``_spawn_codex_coder``."""
    _spawn_codex_coder(coder_run_id, text, resume_session_id=codex_session_id)


def delegate_task_background(
    parent_agent=None,
    goal: Optional[str] = None,
    context: str = "",
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Async variant of delegate_task: spawns the coder detached and returns immediately.

    Returns:
        {"coder_run_id": str, "status": "spawned", "goal": str}

    The gateway is expected to create a Discord thread keyed by coder_run_id
    and route subagent_progress events into that thread.
    """
    import uuid as _uuid

    if parent_agent is None:
        return {"error": "delegate_task_background requires a parent agent context."}
    if not goal:
        return {"error": "delegate_task_background requires a non-empty goal."}

    # Surface missing/expired codex auth as a structured error instead of
    # letting codex fail mid-NDJSON-stream with an opaque returncode.
    from .coder_config import check_codex_auth
    auth_err = check_codex_auth()
    if auth_err:
        return {
            "coder_run_id": None,
            "status": "auth_error",
            "error": auth_err,
            "goal": goal,
        }

    coder_run_id = f"coder-{_uuid.uuid4().hex[:8]}"
    parent_task_id = (
        getattr(parent_agent, "task_id", None)
        or getattr(parent_agent, "_subagent_id", None)
        or "unknown"
    )
    _register_coder_run(coder_run_id, parent_task_id, goal)
    _spawn_detached_coder(
        parent_agent=parent_agent,
        goal=goal,
        context=context,
        coder_run_id=coder_run_id,
        provider=provider or "codex-exec",
    )
    # Fire coder_spawn_callback so the active platform adapter can open a UI
    # surface (e.g. a Discord thread) bound to coder_run_id. Lives here (not in
    # run_agent's inline dispatch) so the registry handler path and any caller
    # get the same behavior — see P1 plan Step 4.7 (agent-loop elif removal).
    spawn_cb = getattr(parent_agent, "coder_spawn_callback", None)
    if coder_run_id and spawn_cb is not None:
        try:
            spawn_cb(coder_run_id, goal or "")
        except Exception as cb_err:
            logger.debug("coder_spawn_callback error: %s", cb_err)
    return {"coder_run_id": coder_run_id, "status": "spawned", "goal": goal}


DELEGATE_TASK_BACKGROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "What the coder should accomplish (specific, self-contained). "
                "The Codex CLI subagent will handle planning, file edits, and "
                "command execution on its own."
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Additional context: file paths, error messages, constraints, "
                "links to related issues."
            ),
        },
    },
    "required": ["goal"],
}


# Registered at module load (i.e. when subagent_coder.register(ctx) does
# ``from . import delegate_background``). Dispatch itself is intercepted by the
# AIAgent._invoke_tool wrap (parent_agent injection); this registration exists
# so the tool schema/check_fn are advertised to the model.
def register_delegate_task_background() -> None:
    """Register the ``delegate_task_background`` tool on the shared registry.

    Run once at import. Exposed as a function (and idempotent via
    ``registry.register``'s overwrite semantics) so tests can re-assert that the
    *this-module* handler is the registered one — a bundled copy of this plugin
    in a dev fork can otherwise re-register an equivalent handler bound to its
    own module globals, which would defeat ``patch`` targets on this module.
    """
    registry.register(
        name="delegate_task_background",
        toolset="delegation",
        schema=DELEGATE_TASK_BACKGROUND_SCHEMA,
        handler=lambda args, **kw: json.dumps(
            delegate_task_background(
                # Sequential path: registry.dispatch passes no parent_agent, so
                # fall back to the ContextVar set by the
                # _execute_tool_calls_sequential wrap. Concurrent path bypasses
                # this handler (the _invoke_tool wrap injects parent_agent=self).
                parent_agent=kw.get("parent_agent") or _dispatch_parent_agent.get(),
                goal=args.get("goal"),
                context=args.get("context") or "",
            ),
            ensure_ascii=False,
        ),
        check_fn=check_delegate_requirements,
        emoji="🧑‍💻",
    )


register_delegate_task_background()
