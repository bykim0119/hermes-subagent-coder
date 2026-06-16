"""subagent_coder plugin — Codex CLI 기반 코더 서브에이전트.

Wires (Task 2~8에서 차례로 채움):
- delegate_task_background tool (registry.register with parent_agent kw)
- codex-exec model provider
- Discord platform overlay (factory wrap + connect wrap)
- AIAgent.coder_spawn_callback slot

자세한 설계: ``coder-fork-isolation/2026-05-23-coder-fork-isolation-design.md``
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Callable, Optional

# Guarded so the module is importable even when loaded *bare* (no parent package)
# — e.g. when pytest scans the flat repo root, whose directory name may not be a
# valid Python identifier. hermes (and the test conftest) always load this with
# ``__package__`` set, so the real wiring imports normally; the bare scan just
# gets a harmless, unused module object.
if __package__:
    from . import codex_provider

logger = logging.getLogger(__name__)

# Per-turn coder_spawn_callback, set by the GatewayRunner._run_agent wrap and
# read by the AIAgent.run_conversation wrap to install it on the agent. Both
# run on the gateway loop thread within one _run_agent coroutine, so the
# ContextVar propagates (the agent object then carries the callback across the
# thread boundary into tool execution). ``None`` outside a gateway turn (e.g.
# CLI), so the run_conversation wrap is a no-op there.
_coder_spawn_cb_ctx: ContextVar[Optional[Callable[[str, str], None]]] = ContextVar(
    "coder_spawn_cb", default=None
)


def register(ctx) -> None:
    """Plugin entry point — Hermes plugin system이 로드 시 ``register(ctx)``로 호출.

    ``ctx``는 ``PluginContext(manifest, manager)`` (hermes_cli/plugins.py).
    """
    logger.info("subagent_coder: register(ctx) started")
    codex_provider.register_codex_provider(ctx)
    _install_codex_exec_auth()
    _install_codex_exec_aux_client_wrap()
    # Import the coder delegation module — this registers the
    # delegate_task_background tool on the registry at import time.
    from . import delegate_background  # noqa: F401
    _install_delegate_dispatch_wrap()
    _install_sequential_dispatch_wrap()
    _install_coder_child_wraps()
    _install_codex_exec_client_factory_wrap()
    _install_coder_spawn_callback_slot()
    _install_gateway_coder_spawn_wraps()
    _install_coder_toolset_membership()
    from . import coder_orchestration
    coder_orchestration.register_orchestration_tools()
    coder_orchestration.install_orchestration_toolset_membership()
    _install_discord_coder_overlay()
    logger.info(
        "subagent_coder: register(ctx) complete "
        "(provider + defaults + delegate_background + dispatch/sequential/child/"
        "client/run_conversation wraps + spawn-callback slot + gateway spawn hook "
        "+ discord overlay)"
    )


def _install_delegate_dispatch_wrap() -> None:
    """Runtime-wrap AIAgent._invoke_tool to inject parent_agent for the coder.

    Why a monkey-patch instead of editing run_agent.py: the registry dispatch
    path (model_tools.handle_function_call -> registry.dispatch) never forwards
    parent_agent to handlers — verified, and upstream's own delegate_task uses
    an inline _dispatch for exactly this reason. By wrapping here at register
    time, the coder works on a STOCK hermes (no run_agent.py edits), which is
    what makes subagent_coder installable as a standalone ~/.hermes/plugins/ unit.

    parent_agent is taken from ``self`` directly (not a ContextVar) so it
    survives the concurrent path's worker threads — ContextVars don't propagate
    across the ThreadPoolExecutor boundary (lesson from coder commit fd0d901a).

    NOTE: covers the concurrent path (_invoke_tool). The sequential path
    (_execute_tool_calls_sequential) dispatches inline and is wired separately.
    """
    import json

    from run_agent import AIAgent

    if getattr(AIAgent, "_subagent_coder_dispatch_wrapped", False):
        return

    _orig_invoke_tool = AIAgent._invoke_tool

    def _wrapped_invoke_tool(self, function_name, function_args, *args, **kwargs):
        if function_name == "delegate_task_background":
            from .delegate_background import delegate_task_background
            return json.dumps(
                delegate_task_background(
                    parent_agent=self,
                    goal=function_args.get("goal"),
                    context=function_args.get("context") or "",
                    role=function_args.get("role"),
                ),
                ensure_ascii=False,
            )
        return _orig_invoke_tool(self, function_name, function_args, *args, **kwargs)

    AIAgent._invoke_tool = _wrapped_invoke_tool
    AIAgent._subagent_coder_dispatch_wrapped = True
    logger.info("subagent_coder: AIAgent._invoke_tool wrapped for coder dispatch")


def _install_coder_child_wraps() -> None:
    """Runtime-wrap the stock child builders so the coder child gets its
    codex-exec provider, chat_completions api_mode, and a ``_subagent_id``
    pinned to ``coder_run_id`` — without editing tools/delegate_tool.py.

    Stock ``delegate_task`` has no ``override_provider``/``subagent_id_override``
    params, so the coder can't pass them. Instead ``_spawn_detached_coder`` sets
    the ``_coder_child_ctx`` ContextVar and these two wraps read it:

      * ``_build_child_agent``: PRE-inject ``override_provider``/``override_api_mode``
        (codex-exec is process-backed and only does chat.completions — inheriting
        the parent's codex_responses mode crashes the facade), then POST-pin
        ``child._subagent_id`` so ``_run_single_child``/``interrupt_subagent``/
        Discord routing all key off ``coder_run_id``.
      * ``_build_child_progress_callback``: replace the internally generated
        ``subagent_id`` with ``coder_run_id`` so every relayed event routes to
        the matching Discord thread.

    Both are module-level names in tools.delegate_tool, so the internal calls
    inside ``_build_child_agent`` resolve to the wrapped versions at call time.
    The coder spawn is single-task → ``_build_child_agent``, the callback, and
    ``_run_single_child`` all run on the ``_runner`` thread inline, so the
    ContextVar propagates (no ThreadPoolExecutor boundary).
    """
    import tools.delegate_tool as dt
    from .delegate_background import _coder_child_ctx

    if getattr(dt, "_subagent_coder_child_wrapped", False):
        return

    _orig_build_child_agent = dt._build_child_agent
    _orig_build_progress_cb = dt._build_child_progress_callback

    def _wrapped_build_child_agent(*args, **kwargs):
        ctx = _coder_child_ctx.get()
        if ctx is not None:
            kwargs["override_provider"] = ctx["provider"]
            kwargs["override_api_mode"] = ctx["api_mode"]
        child = _orig_build_child_agent(*args, **kwargs)
        if ctx is not None:
            child._subagent_id = ctx["subagent_id"]
        return child

    def _wrapped_build_progress_cb(*args, **kwargs):
        ctx = _coder_child_ctx.get()
        if ctx is not None and "subagent_id" in kwargs:
            kwargs["subagent_id"] = ctx["subagent_id"]
        return _orig_build_progress_cb(*args, **kwargs)

    dt._build_child_agent = _wrapped_build_child_agent
    dt._build_child_progress_callback = _wrapped_build_progress_cb
    dt._subagent_coder_child_wrapped = True
    logger.info(
        "subagent_coder: _build_child_agent / _build_child_progress_callback wrapped"
    )


def _install_sequential_dispatch_wrap() -> None:
    """Runtime-wrap AIAgent._execute_tool_calls_sequential to expose ``self``
    to the coder's registry handler on the sequential dispatch path.

    The concurrent path goes through ``_invoke_tool`` (wrapped separately, which
    injects parent_agent=self directly). The sequential path inlines tool
    dispatch and routes unknown registry tools to ``handle_function_call`` ->
    ``registry.dispatch`` -> handler, which never receives parent_agent. So we
    set the ``_dispatch_parent_agent`` ContextVar to ``self`` around the loop;
    the delegate_task_background handler reads it as a fallback. Single tool
    calls (the typical coder spawn) use this sequential path, so this wrap is
    what makes coder delegation work off the stock run_agent.py.
    """
    from run_agent import AIAgent
    from .delegate_background import _dispatch_parent_agent

    if getattr(AIAgent, "_subagent_coder_sequential_wrapped", False):
        return

    _orig_sequential = AIAgent._execute_tool_calls_sequential

    def _wrapped_sequential(self, *args, **kwargs):
        token = _dispatch_parent_agent.set(self)
        try:
            return _orig_sequential(self, *args, **kwargs)
        finally:
            _dispatch_parent_agent.reset(token)

    AIAgent._execute_tool_calls_sequential = _wrapped_sequential
    AIAgent._subagent_coder_sequential_wrapped = True
    logger.info("subagent_coder: AIAgent._execute_tool_calls_sequential wrapped")


def _install_codex_exec_client_factory_wrap() -> None:
    """Runtime-wrap AIAgent._create_openai_client so codex-exec agents get a
    CodexExecFacade instead of an HTTP client.

    codex-exec is process-backed — ``base_url`` (``codex-exec://local``) is a
    marker, not an endpoint. The facade makes ``chat.completions.create()``
    spawn ``codex exec --json``. Mirrors the stock copilot-acp branch in
    ``_create_openai_client``; lifting it into the plugin keeps run_agent.py
    diff-free.
    """
    from run_agent import AIAgent

    if getattr(AIAgent, "_subagent_coder_client_factory_wrapped", False):
        return

    _orig_create_client = AIAgent._create_openai_client

    def _wrapped_create_client(self, client_kwargs, *, reason, shared):
        if self.provider == "codex-exec" or str(
            client_kwargs.get("base_url", "")
        ).startswith("codex-exec://"):
            from .codex_exec_client import CodexExecFacade
            try:
                from hermes_cli.auth import resolve_external_process_provider_credentials
                _creds = resolve_external_process_provider_credentials("codex-exec")
            except Exception as _e:
                logger.warning("codex-exec credential resolution failed: %s", _e)
                _creds = {}
            client = CodexExecFacade(
                api_key=client_kwargs.get("api_key"),
                base_url=client_kwargs.get("base_url"),
                command=_creds.get("command"),
                args=_creds.get("args") or [],
                subagent_id=getattr(self, "_subagent_id", None),
            )
            logger.info(
                "Codex-exec facade created (%s, shared=%s) %s",
                reason,
                shared,
                self._client_log_context(),
            )
            return client
        return _orig_create_client(self, client_kwargs, reason=reason, shared=shared)

    AIAgent._create_openai_client = _wrapped_create_client
    AIAgent._subagent_coder_client_factory_wrapped = True
    logger.info("subagent_coder: AIAgent._create_openai_client wrapped for codex-exec")


def _install_coder_spawn_callback_slot() -> None:
    """Provide a class-level ``coder_spawn_callback`` default on AIAgent.

    Stock run_agent.py no longer initializes the per-instance slot. gateway/run.py
    sets it per-turn (instance attr) and delegate_task_background reads it via
    ``getattr(parent_agent, "coder_spawn_callback", None)``. The class default
    keeps the attribute introspectable and the getattr cheap. Signature:
    ``(coder_run_id: str, goal: str) -> None``.
    """
    from run_agent import AIAgent

    if "coder_spawn_callback" not in vars(AIAgent):
        AIAgent.coder_spawn_callback = None
        logger.info("subagent_coder: AIAgent.coder_spawn_callback slot installed")


def _build_coder_spawn_callback(runner, source, session_key, run_generation, loop):
    """Build the per-turn coder_spawn_callback closure from gateway context.

    Mirrors the stock gateway/run.py ``_coder_spawn`` exactly: when a coder is
    spawned, open a platform UI surface (Discord thread) bound to coder_run_id
    via ``run_coroutine_threadsafe`` on the captured loop. No-op when the active
    adapter doesn't implement ``create_coder_thread`` or the run is stale.
    """
    import asyncio

    status_adapter = runner.adapters.get(source.platform)
    status_chat_id = source.chat_id
    parent_thread_id = source.thread_id

    def _run_still_current() -> bool:
        if run_generation is None or not session_key:
            return True
        return runner._is_session_run_current(session_key, run_generation)

    def _coder_spawn(coder_run_id: str, goal: str) -> None:
        # 라우팅 메타데이터를 먼저 기록 — 스레드 생성 가드와 무관하게 완료 웨이크가
        # 동작하도록(메인 세션 source/loop는 이 클로저에 이미 캡처됨).
        try:
            from .delegate_background import record_main_routing
            record_main_routing(coder_run_id, source, loop)
        except Exception:
            logger.debug("record_main_routing failed", exc_info=True)
        if not status_adapter or not _run_still_current():
            return
        if not hasattr(status_adapter, "create_coder_thread"):
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                status_adapter.create_coder_thread(
                    coder_run_id=coder_run_id,
                    goal=goal,
                    chat_id=status_chat_id,
                    parent_thread_id=parent_thread_id,
                ),
                loop,
            )

            # run_coroutine_threadsafe returns a future that is otherwise never
            # awaited, so create_coder_thread exceptions would vanish silently.
            # Surface them at debug level for diagnosability.
            def _log_thread_result(f):
                try:
                    f.result()
                except Exception:
                    logger.debug("create_coder_thread raised", exc_info=True)

            fut.add_done_callback(_log_thread_result)
        except Exception as _e:
            logger.debug("coder_spawn_callback error: %s", _e)

    return _coder_spawn


def _install_gateway_coder_spawn_wraps() -> None:
    """Install coder_spawn_callback per-turn without editing gateway/run.py.

    Two wraps cooperating via the _coder_spawn_cb_ctx ContextVar:
      * GatewayRunner._run_agent (async, runs on the gateway loop thread): builds
        the per-turn _coder_spawn closure from the turn's context (adapter, loop,
        chat_id, thread_id, generation — all derivable from self + args) and sets
        the ContextVar around the orig call.
      * AIAgent.run_conversation: called synchronously inside _run_agent on the
        same loop thread/context, so it reads the ContextVar and pins
        ``self.coder_spawn_callback`` onto the (cached or fresh) agent. The attr
        then crosses the thread boundary into tool execution where
        delegate_task_background reads it.

    GatewayRunner lives in gateway.run (gateway-only, heavy). We only wrap it when
    that module is already imported — in gateway mode plugin discovery is first
    triggered mid-turn (tools_config), long after gateway.run loads, so the guard
    reliably finds it. In CLI mode gateway.run is absent and the run_conversation
    wrap stays a harmless no-op (the ContextVar is never set).
    """
    import sys

    from run_agent import AIAgent

    # run_conversation wrap — always safe to install (run_agent is core).
    if not getattr(AIAgent, "_subagent_coder_run_conversation_wrapped", False):
        _orig_run_conversation = AIAgent.run_conversation

        def _wrapped_run_conversation(self, *args, **kwargs):
            cb = _coder_spawn_cb_ctx.get()
            if cb is not None:
                self.coder_spawn_callback = cb
            return _orig_run_conversation(self, *args, **kwargs)

        AIAgent.run_conversation = _wrapped_run_conversation
        AIAgent._subagent_coder_run_conversation_wrapped = True
        logger.info("subagent_coder: AIAgent.run_conversation wrapped for spawn-callback install")

    # GatewayRunner._run_agent wrap — only in gateway mode.
    # Plugin discovery/register can run *before* the CLI lazily imports
    # gateway.run (observed: register at boot, gateway.run loaded ~45s later).
    # A bare sys.modules check therefore misfires as "CLI mode" and the
    # _run_agent wrap (which sets the per-turn coder_spawn_callback ContextVar)
    # is permanently skipped — so delegate_task_background never opens a Discord
    # thread. Use the launch command (`hermes gateway run`) as the reliable
    # signal and ensure-import gateway.run so GatewayRunner exists to wrap.
    # (Same timing fix as the Discord overlay; wrapping the class persists for
    # the GatewayRunner instance created later at gateway start.)
    gw = sys.modules.get("gateway.run")
    if gw is None and "gateway" in sys.argv:
        try:
            import gateway.run as gw  # noqa: F811
        except Exception:
            logger.debug(
                "subagent_coder: could not import gateway.run — skipping _run_agent wrap",
                exc_info=True,
            )
            return
    GatewayRunner = getattr(gw, "GatewayRunner", None) if gw is not None else None
    if GatewayRunner is None:
        logger.debug("subagent_coder: gateway.run not loaded — skipping _run_agent wrap (CLI mode)")
        return
    if getattr(GatewayRunner, "_subagent_coder_run_agent_wrapped", False):
        return

    import asyncio
    import inspect

    _orig_run_agent = GatewayRunner._run_agent
    _run_agent_sig = inspect.signature(_orig_run_agent)

    async def _wrapped_run_agent(self, *args, **kwargs):
        token = None
        try:
            bound = _run_agent_sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            source = bound.arguments.get("source")
            session_key = bound.arguments.get("session_key")
            run_generation = bound.arguments.get("run_generation")
            if source is not None:
                cb = _build_coder_spawn_callback(
                    self, source, session_key, run_generation,
                    asyncio.get_running_loop(),
                )
                token = _coder_spawn_cb_ctx.set(cb)
        except Exception:
            logger.debug("subagent_coder: failed to build coder_spawn_callback", exc_info=True)
        try:
            return await _orig_run_agent(self, *args, **kwargs)
        finally:
            if token is not None:
                _coder_spawn_cb_ctx.reset(token)

    GatewayRunner._run_agent = _wrapped_run_agent
    GatewayRunner._subagent_coder_run_agent_wrapped = True
    logger.info("subagent_coder: GatewayRunner._run_agent wrapped for coder_spawn_callback")


def _install_coder_toolset_membership() -> None:
    """Make delegate_task_background a member of every toolset that offers
    delegate_task — without editing toolsets.py.

    Stock toolsets.py listed delegate_task_background in ``_HERMES_CORE_TOOLS``
    and the ``delegation`` toolset. We replicate that at register time:

      * ``_HERMES_CORE_TOOLS`` is mutated in place, so all toolsets that hold it
        by reference (hermes-cli, hermes-cron, hermes-telegram, ...) pick it up
        live via resolve_toolset.
      * A few toolsets (hermes-discord, hermes-feishu, hermes-yuanbao) and the
        ``delegation`` toolset store SEPARATE lists (built with
        ``_HERMES_CORE_TOOLS + [...]`` at import, or their own literal). Those
        copies don't see the in-place mutation, so we scan TOOLSETS and add
        delegate_task_background to any list that offers delegate_task but not
        the background variant. resolve_toolset set()-ifies tools, so ordering
        is irrelevant; the membership is what matters.
    """
    import toolsets

    core = toolsets._HERMES_CORE_TOOLS
    if "delegate_task_background" not in core:
        idx = (core.index("delegate_task") + 1) if "delegate_task" in core else len(core)
        core.insert(idx, "delegate_task_background")

    for ts in toolsets.TOOLSETS.values():
        tools = ts.get("tools")
        if tools is core or not isinstance(tools, list):
            continue  # by-reference (already mutated) or non-list
        if "delegate_task" in tools and "delegate_task_background" not in tools:
            tools.append("delegate_task_background")

    logger.info("subagent_coder: delegate_task_background added to delegation toolsets")


_CODEX_EXEC_BASE_URL = "codex-exec://local"
_CODEX_EXEC_DEFAULT_ARGS = [
    "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write",
]


def _resolve_codex_exec_credentials() -> dict:
    """Resolve codex-exec external-process credentials (plugin-owned).

    Replicates the data-driven resolution stock auth.py uses for copilot-acp,
    but for codex-exec: command from HERMES_CODER_COMMAND (else ``codex``), args
    from HERMES_CODER_ARGS (else the default exec args), base_url from
    CODEX_EXEC_BASE_URL (else codex-exec://local). codex-exec has no remote
    transport, so a missing CLI is always fatal.
    """
    import os
    import shlex
    import shutil

    from hermes_cli.auth import AuthError, PROVIDER_REGISTRY

    pconfig = PROVIDER_REGISTRY.get("codex-exec")
    base_url = ""
    if pconfig is not None and pconfig.base_url_env_var:
        base_url = os.getenv(pconfig.base_url_env_var, "").strip()
    if not base_url:
        base_url = pconfig.inference_base_url if pconfig is not None else _CODEX_EXEC_BASE_URL

    command = os.getenv("HERMES_CODER_COMMAND", "").strip() or "codex"
    raw_args = os.getenv("HERMES_CODER_ARGS", "").strip()
    args = shlex.split(raw_args) if raw_args else list(_CODEX_EXEC_DEFAULT_ARGS)

    resolved_command = shutil.which(command) if command else None
    if not resolved_command:
        raise AuthError(
            f"Could not find the CLI command '{command}'. "
            "Install OpenAI Codex CLI or set HERMES_CODER_COMMAND.",
            provider="codex-exec",
            code="missing_codex_cli",
        )

    return {
        "provider": "codex-exec",
        "api_key": "codex-exec",
        "base_url": base_url.rstrip("/"),
        "command": resolved_command or command,
        "args": args,
        "source": "process",
    }


def _install_discord_coder_overlay() -> None:
    """Attach the coder overlay onto DiscordAdapter — without editing discord.py.

    The overlay setattrs the coder helper methods (create_coder_thread,
    on_coder_event, /code handler, ...) and wraps a few stock methods
    (__init__, _run_post_connect_initialization, disconnect, _handle_message,
    _register_slash_commands) to restore every coder behavior that used to live
    inline in ``gateway/platforms/discord.py``.

    Plugin discovery timing vs. the Discord adapter lifecycle is not fixed:
    hermes may discover plugins before ``_create_adapter`` imports the discord
    platform module, or lazily on the first turn after the adapter already
    connected. So we only gate on *gateway mode* here (``gateway.run`` loaded);
    ``install_discord_coder_overlay`` then ensure-imports the platform module so
    the class exists to wrap, and retrofits a live adapter if one already
    connected. In CLI mode (no gateway) we skip without importing discord.py.
    """
    import sys

    # Detect gateway mode. Plugin discovery can run *before* gateway.run is
    # imported (the CLI imports gateway.run lazily, after discovery), so a
    # sys.modules check alone misfires as "CLI mode" and skips the overlay
    # entirely. The launch command (`hermes gateway run`) is the reliable
    # signal, so also consult sys.argv.
    _gateway_mode = (
        "plugins.platforms.discord.adapter" in sys.modules  # hermes >=0.16
        or "gateway.platforms.discord" in sys.modules  # hermes <0.16
        or "gateway.run" in sys.modules
        or "gateway" in sys.argv
    )
    if not _gateway_mode:
        logger.debug(
            "subagent_coder: not in gateway mode — skipping Discord overlay (CLI mode)"
        )
        return

    from .discord_overlay import install_discord_coder_overlay

    install_discord_coder_overlay()


def _install_codex_exec_auth() -> None:
    """Register codex-exec in hermes_cli.auth without editing auth.py.

    Stock auth.py knows nothing about codex-exec and its
    resolve_external_process_provider_credentials is hardcoded for copilot-acp.
    We (1) add a codex-exec ProviderConfig to PROVIDER_REGISTRY so config
    lookups behave like upstream, and (2) wrap the resolver so codex-exec is
    resolved by this plugin while everything else falls through to stock.
    """
    import hermes_cli.auth as auth

    if "codex-exec" not in auth.PROVIDER_REGISTRY:
        auth.PROVIDER_REGISTRY["codex-exec"] = auth.ProviderConfig(
            id="codex-exec",
            name="OpenAI Codex CLI",
            auth_type="external_process",
            inference_base_url=_CODEX_EXEC_BASE_URL,
            base_url_env_var="CODEX_EXEC_BASE_URL",
        )

    if getattr(auth, "_subagent_coder_resolver_wrapped", False):
        return

    _orig_resolve = auth.resolve_external_process_provider_credentials

    def _wrapped_resolve(provider_id: str):
        if provider_id == "codex-exec":
            return _resolve_codex_exec_credentials()
        return _orig_resolve(provider_id)

    auth.resolve_external_process_provider_credentials = _wrapped_resolve
    auth._subagent_coder_resolver_wrapped = True
    logger.info("subagent_coder: codex-exec registered + resolve_external_process wrapped")


def _install_codex_exec_aux_client_wrap() -> None:
    """Teach agent.auxiliary_client.resolve_provider_client about codex-exec.

    Stock resolve_provider_client handles copilot-acp under external_process and
    falls through to "not directly supported" for codex-exec. We wrap it so a
    codex-exec request builds a CodexExecFacade (mirroring the stock copilot-acp
    branch), leaving every other provider to stock. Callers import the function
    lazily, so they pick up the wrapped version.
    """
    from agent import auxiliary_client as aux

    if getattr(aux, "_subagent_coder_aux_client_wrapped", False):
        return

    _orig_resolve_provider_client = aux.resolve_provider_client

    def _wrapped_resolve_provider_client(provider, *args, **kwargs):
        if provider == "codex-exec":
            return _resolve_codex_exec_aux_client(provider, *args, **kwargs)
        return _orig_resolve_provider_client(provider, *args, **kwargs)

    aux.resolve_provider_client = _wrapped_resolve_provider_client
    aux._subagent_coder_aux_client_wrapped = True
    logger.info("subagent_coder: auxiliary_client.resolve_provider_client wrapped for codex-exec")


def _resolve_codex_exec_aux_client(
    provider,
    model=None,
    async_mode=False,
    raw_codex=False,
    explicit_base_url=None,
    explicit_api_key=None,
    api_mode=None,
    main_runtime=None,
    is_vision=False,
):
    """codex-exec branch of resolve_provider_client, lifted into the plugin.

    Mirrors the stock external_process flow: resolve creds, normalize the model,
    validate, build the CodexExecFacade, async-wrap if requested.
    """
    from hermes_cli.auth import resolve_external_process_provider_credentials
    from agent.auxiliary_client import (
        _normalize_resolved_model,
        _read_main_model,
        _to_async_client,
    )

    creds = resolve_external_process_provider_credentials(provider)
    final_model = _normalize_resolved_model(
        model
        or (main_runtime.get("model") if main_runtime else None)
        or _read_main_model(),
        provider,
    )
    api_key = str(creds.get("api_key", "")).strip()
    base_url = str(creds.get("base_url", "")).strip()
    command = str(creds.get("command", "")).strip() or None
    cmd_args = list(creds.get("args") or [])
    if not final_model:
        logger.warning(
            "resolve_provider_client: codex-exec requested but no model "
            "was provided or configured"
        )
        return None, None
    if not api_key or not base_url:
        logger.warning(
            "resolve_provider_client: codex-exec requested but external "
            "process credentials are incomplete"
        )
        return None, None

    from .codex_exec_client import CodexExecFacade

    client = CodexExecFacade(
        api_key=api_key,
        base_url=base_url,
        command=command,
        args=cmd_args,
    )
    logger.debug("resolve_provider_client: %s (%s)", provider, final_model)
    return (
        _to_async_client(client, final_model, is_vision=is_vision)
        if async_mode
        else (client, final_model)
    )
