"""Discord platform overlay for the coder sub-agent.

Externalizes every coder-specific addition that previously lived inline in
``gateway/platforms/discord.py`` so that stock file carries zero coder code.
``install_discord_coder_overlay()`` (called from ``register(ctx)``) attaches the
coder helper methods onto ``DiscordAdapter`` via ``setattr`` and wraps a handful
of stock methods to add coder behavior without editing the stock module:

  * ``__init__``                       POST-wrap → create ``_coder_sessions`` + ``_coder_flusher`` slot
  * ``_run_post_connect_initialization`` POST-wrap → start the progress flusher + register on the coder event bus
  * ``disconnect``                     PRE-wrap → detach from the bus + clear global sessions
  * ``_handle_message``                wrap → route messages in coder-bound threads to the coder (cancel / follow-up)
  * ``_register_slash_commands``       POST-wrap → add the ``/code`` slash command

Because ``DiscordAdapter`` is instantiated only after ``discover_plugins()`` runs
``register(ctx)`` (gateway/run.py: discover < _create_adapter), the class-level
wraps/setattrs are in place before any adapter exists. The install is guarded on
``gateway.platforms.discord`` already being imported (gateway-only, heavy), so it
is a no-op in CLI mode.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import discord

from .coder_progress_formatter import format_event as _format_coder_event

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Coder sub-agent helper methods (attached onto DiscordAdapter via setattr).
# Each takes ``self`` (the adapter) as first arg — identical bodies to the
# former inline ``gateway/platforms/discord.py`` methods.
# ----------------------------------------------------------------------

def _make_thread_name(self, goal: str) -> str:
    """Sanitize a coder goal into a Discord thread name (cap 60 chars)."""
    name = " ".join((goal or "coder").split())
    name = name.replace("`", "").replace("\n", " ").strip()
    return name[:60] if len(name) > 60 else (name or "coder")


async def _publish_to_thread(self, thread_id: str, body: str) -> None:
    """Publish a (possibly multi-line) message to a Discord thread by id."""
    if not body or self._client is None:
        return
    try:
        channel = self._client.get_channel(int(thread_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(thread_id))
        await channel.send(content=body[:1900])
    except Exception as exc:
        logger.warning(
            "[%s] Failed to publish to coder thread %s: %s",
            self.name,
            thread_id,
            exc,
        )


async def create_coder_thread(
    self,
    coder_run_id: str,
    goal: str,
    chat_id: str,
    parent_thread_id: Optional[str] = None,
) -> None:
    """Open a Discord thread bound to a coder_run_id.

    Called from gateway/run.py via ``coder_spawn_callback`` when the LLM
    invokes ``delegate_task_background``. Sends an anchor message, opens a
    thread off it, and registers the binding in ``_coder_sessions`` so
    Phase-2 progress routing (subagent_progress events) and follow-up
    replies in the thread can resolve back to the right coder run.

    If the user mentioned Hermes inside an existing thread, the new coder
    thread is created off the *parent* channel — Discord doesn't allow
    nested threads.
    """
    if self._client is None:
        return
    try:
        target_id = chat_id
        channel = self._client.get_channel(int(target_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(target_id))
        if isinstance(channel, discord.Thread):
            channel = channel.parent or channel
            if channel is None:
                logger.warning(
                    "[%s] Cannot create coder thread: parent channel missing for %s",
                    self.name, target_id,
                )
                return
        anchor = await channel.send(f"▶ 코더에게 위임 — `{coder_run_id}`")
        thread_name = self._make_thread_name(goal)
        thread = await anchor.create_thread(
            name=thread_name,
            auto_archive_duration=1440,
        )
        try:
            self._coder_sessions.bind(
                coder_run_id=coder_run_id,
                thread_id=str(thread.id),
                parent_channel_id=str(channel.id),
            )
        except ValueError as exc:
            # max_concurrent guard tripped — let the user know in the
            # anchor channel and abandon the thread (it will auto-archive).
            await channel.send(f"⚠️ {exc}")
            return
        try:
            self._threads.mark_participated(str(thread.id))
        except Exception:
            pass
    except Exception as exc:
        logger.exception(
            "[%s] Failed to create coder thread for %s: %s",
            self.name, coder_run_id, exc,
        )


async def on_coder_event(self, subagent_id: str, event: dict) -> None:
    """Route a coder NDJSON event to the bound Discord thread.

    Invoked via ``plugins.subagent_coder.coder_event_bus`` from the coder sink (which
    lives in a background daemon thread spawned by delegate_task_background
    or by ``_handle_coder_followup``). Lookup is cheap and tolerant —
    unknown coder_run_ids drop silently because a thread bind may not yet
    have committed when the first events stream in (or the coder finished
    before the bind), and either case is recoverable on the next event.
    """
    if not subagent_id or not event:
        return
    thread_id = self._coder_sessions.get_thread(subagent_id)
    if not thread_id:
        return
    text = _format_coder_event(event)
    if not text:
        return
    if self._coder_flusher is None:
        await self._publish_to_thread(thread_id, text)
        return
    await self._coder_flusher.add(thread_id, text)
    try:
        self._coder_sessions.touch(subagent_id)
    except Exception:
        pass


async def _handle_code_slash(
    self,
    interaction: 'discord.Interaction',
    task: str,
) -> None:
    """Handle ``/code <task>`` — spawn a fresh coder thread without going
    through the main Hermes turn.

    This is the deterministic shortcut for coding delegation: the LLM
    sometimes picks ``delegate_task`` (in-turn) instead of
    ``delegate_task_background`` even with the AGENTS.md guide, so this
    slash bypasses LLM tool selection entirely. End result is identical
    to a successful natural-language delegation — same thread anchor,
    same coder bus routing, same follow-up support.

    Mirrors the follow-up path's "parent_agent-free spawn" pattern: we
    call ``_spawn_codex_coder`` directly (no resume) instead of going
    through delegate_task_background → AIAgent → CodexExecFacade.
    """
    if not await self._check_slash_authorization(interaction, "/code"):
        return
    if not task or not task.strip():
        await interaction.response.send_message(
            "Usage: `/code <task>` — describe the coding task to delegate.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Pre-check codex auth so the user sees a specific message instead
    # of an opaque process failure inside the thread.
    try:
        from .coder_config import check_codex_auth
        auth_err = check_codex_auth()
    except Exception:
        auth_err = None  # never block on the pre-check itself
    if auth_err:
        await interaction.followup.send(f"❌ {auth_err}", ephemeral=True)
        return

    import uuid as _uuid

    coder_run_id = f"coder-{_uuid.uuid4().hex[:8]}"
    parent_task_id = f"slash:/code:{interaction.user.id}"

    try:
        from .delegate_background import _register_coder_run, _spawn_codex_coder
    except Exception as exc:
        logger.exception("[%s] /code import failed: %s", self.name, exc)
        await interaction.followup.send(f"❌ /code import 실패: {exc}", ephemeral=True)
        return

    _register_coder_run(coder_run_id, parent_task_id, task)

    # Create thread (anchor message + bind to coder_sessions). If we're
    # inside an existing thread, anchor in the parent channel — Discord
    # disallows nested threads.
    try:
        channel = interaction.channel
        chat_id = str(channel.id)
        parent_thread_id = None
        if isinstance(channel, discord.Thread):
            parent_thread_id = chat_id
            parent_channel = channel.parent
            if parent_channel is not None:
                chat_id = str(parent_channel.id)
        await self.create_coder_thread(
            coder_run_id=coder_run_id,
            goal=task,
            chat_id=chat_id,
            parent_thread_id=parent_thread_id,
        )
    except Exception as exc:
        logger.exception("[%s] /code thread creation failed: %s", self.name, exc)
        await interaction.followup.send(
            f"❌ 스레드 생성 실패: {exc}", ephemeral=True
        )
        return

    # Spawn the coder (fresh — no resume_session_id). thread.started will
    # populate codex_session_id so follow-up messages in the thread work.
    try:
        _spawn_codex_coder(coder_run_id=coder_run_id, text=task)
    except Exception as exc:
        logger.exception("[%s] /code spawn failed: %s", self.name, exc)
        await interaction.followup.send(
            f"❌ 코더 시작 실패: {exc}", ephemeral=True
        )
        return

    # Clean up the ephemeral defer; the public anchor + thread are now
    # carrying the conversation.
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def _cancel_coder_run(
    self,
    coder_run_id: str,
    thread: Any,
) -> None:
    """Cancel an active coder run from inside its Discord thread.

    Triggered when ``is_cancel_command`` matches a thread message. We
    SIGTERM the codex process group via ``cancel_coder_run`` and post
    a terminal message. The session binding is removed so any race
    with a late ``thread.completed`` event doesn't re-touch the slot.
    We do NOT delete or archive the thread — the user can scroll the
    captured progress, which is usually why they cancelled.
    """
    try:
        from .delegate_background import cancel_coder_run
    except Exception as exc:
        logger.exception(
            "[%s] cancel import failed for %s: %s",
            self.name, coder_run_id, exc,
        )
        try:
            await thread.send(f"❌ 취소 import 실패: {exc}")
        except Exception:
            pass
        return

    ok = bool(cancel_coder_run(coder_run_id))
    try:
        if ok:
            await thread.send("❌ 취소됨")
        else:
            await thread.send(
                f"⚠️ 취소 시도 — 코더(`{coder_run_id}`)가 이미 종료/미등록"
            )
    except Exception:
        logger.debug("cancel announce failed", exc_info=True)
    try:
        self._coder_sessions.unbind(coder_run_id)
    except Exception:
        logger.debug("coder_sessions.unbind failed", exc_info=True)


async def _handle_coder_followup(
    self,
    coder_run_id: str,
    text: str,
    thread: Any,
) -> None:
    """Forward a message in a coder-bound thread to ``codex exec resume``.

    Uses the codex session UUID captured from the first spawn's
    ``thread.started`` event to re-enter the same conversation context
    (codex 0.121.0+). If the UUID is missing — older codex, eviction race,
    or sandbox-blocked startup — we tell the user and abandon: a cold
    spawn would silently lose the prior workspace state, which is worse
    than an explicit error.
    """
    if not text or not text.strip():
        return
    try:
        from .coder_config import check_codex_auth
        auth_err = check_codex_auth()
    except Exception:
        auth_err = None
    if auth_err:
        try:
            await thread.send(f"❌ {auth_err}")
        except Exception:
            pass
        return
    codex_session_id = self._coder_sessions.get_codex_session_id(coder_run_id)
    if not codex_session_id:
        try:
            await thread.send(
                "⚠️ 코더 세션 UUID 미기록 — `codex exec resume` 불가. "
                "새 위임으로 시작해주세요."
            )
        except Exception:
            pass
        return

    try:
        from .delegate_background import _spawn_followup_coder

        _spawn_followup_coder(
            coder_run_id=coder_run_id,
            codex_session_id=codex_session_id,
            text=text,
        )
    except Exception as exc:
        logger.exception(
            "[%s] Failed to spawn coder follow-up for %s: %s",
            self.name, coder_run_id, exc,
        )
        try:
            await thread.send(f"❌ follow-up spawn 실패: {exc}")
        except Exception:
            pass


# ----------------------------------------------------------------------
# Idempotent coder-state helpers (shared by the wraps and the late retrofit).
# ----------------------------------------------------------------------

def _ensure_coder_state(self) -> None:
    """Create ``_coder_sessions`` + ``_coder_flusher`` on the adapter if absent."""
    if getattr(self, "_coder_sessions", None) is None:
        from .coder_config import coder_setting
        from .coder_sessions import CoderSessionManager
        self._coder_sessions = CoderSessionManager(
            idle_timeout_seconds=coder_setting(
                "idle_timeout_seconds",
                env_var="HERMES_CODER_IDLE_TIMEOUT_S",
                default=7200,
                cast=int,
            ),
            max_concurrent=coder_setting(
                "max_concurrent",
                env_var="HERMES_CODER_MAX_CONCURRENT",
                default=3,
                cast=int,
            ),
        )
    if not hasattr(self, "_coder_flusher"):
        self._coder_flusher = None  # set in post-connect init


def _start_coder_progress(self) -> None:
    """Start the progress debouncer + register on the coder event bus.

    Must run inside the adapter's event loop (uses ``get_running_loop``).
    Idempotent: the flusher is created once, and the bus handler is keyed on
    ``self.on_coder_event``.
    """
    import asyncio

    _ensure_coder_state(self)
    if getattr(self, "_coder_flusher", None) is None:
        from .coder_config import coder_setting
        from .coder_progress_formatter import DebouncedFlusher
        self._coder_flusher = DebouncedFlusher(
            interval_ms=coder_setting(
                "progress_debounce_ms",
                env_var="HERMES_CODER_DEBOUNCE_MS",
                default=250,
                cast=int,
            ),
            publish=self._publish_to_thread,
        )
        self._coder_flusher.start()
    # Publish this adapter's coder hook to the gateway-level bus so the coder
    # sink (background thread, outside any parent agent turn) can route NDJSON
    # events to our threads. set_global_sessions exposes our CoderSessionManager
    # so the sink captures codex session UUIDs.
    try:
        from . import coder_event_bus
        from .coder_sessions import set_global_sessions

        set_global_sessions(self._coder_sessions)
        coder_event_bus.register_handler(
            self.on_coder_event,
            asyncio.get_running_loop(),
        )
    except Exception as _e:
        logger.debug("[%s] coder_event_bus register failed: %s", self.name, _e)


def _add_code_command(self) -> None:
    """Add the ``/code`` slash command to the client tree (idempotent)."""
    client = getattr(self, "_client", None)
    if client is None:
        return
    tree = client.tree
    try:
        if tree.get_command("code") is not None:
            return
    except Exception:
        pass

    @tree.command(name="code", description="Spawn a coder sub-agent for this task")
    @discord.app_commands.describe(task="The coding task to delegate to the coder")
    async def slash_code(interaction: discord.Interaction, task: str):
        await self._handle_code_slash(interaction, task)


# ----------------------------------------------------------------------
# Install: setattr helper methods + wrap stock methods + late retrofit.
# ----------------------------------------------------------------------

def install_discord_coder_overlay() -> None:
    """Attach coder methods + wraps onto ``DiscordAdapter`` (gateway mode only).

    No-op when ``gateway.platforms.discord`` is not imported (CLI mode) or
    when already installed (idempotent via a class sentinel). In gateway mode,
    plugin discovery can run *after* the adapter has already connected (hermes
    discovers plugins lazily on the first turn), so after installing the class
    wraps we also retrofit any live, already-connected adapter instance.
    """
    disc = sys.modules.get("gateway.platforms.discord")
    DiscordAdapter = getattr(disc, "DiscordAdapter", None) if disc is not None else None
    if DiscordAdapter is None:
        logger.debug(
            "subagent_coder: gateway.platforms.discord not loaded — skipping Discord overlay (CLI mode)"
        )
        return
    if getattr(DiscordAdapter, "_subagent_coder_overlay_installed", False):
        _retrofit_live_discord_adapter()  # discovery may re-run; ensure live wiring
        return

    # 1. Attach coder helper methods onto the adapter class.
    DiscordAdapter._make_thread_name = _make_thread_name
    DiscordAdapter._publish_to_thread = _publish_to_thread
    DiscordAdapter.create_coder_thread = create_coder_thread
    DiscordAdapter.on_coder_event = on_coder_event
    DiscordAdapter._handle_code_slash = _handle_code_slash
    DiscordAdapter._cancel_coder_run = _cancel_coder_run
    DiscordAdapter._handle_coder_followup = _handle_coder_followup

    # 2. __init__ POST-wrap: create the coder session manager + flusher slot.
    _orig_init = DiscordAdapter.__init__

    def _wrapped_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        _ensure_coder_state(self)

    DiscordAdapter.__init__ = _wrapped_init

    # 3. _run_post_connect_initialization POST-wrap: start progress flusher +
    #    register on the coder event bus. Runs on the gateway loop thread
    #    (this coroutine is scheduled as a task from on_ready), so
    #    get_running_loop() is valid. Idempotent via the flusher None check.
    _orig_post_connect = DiscordAdapter._run_post_connect_initialization

    async def _wrapped_post_connect(self, *args, **kwargs):
        result = await _orig_post_connect(self, *args, **kwargs)
        _start_coder_progress(self)
        return result

    DiscordAdapter._run_post_connect_initialization = _wrapped_post_connect

    # 4. disconnect PRE-wrap: detach from the coder event bus + global
    #    sessions pointer so a stale handler can't be invoked after disconnect.
    _orig_disconnect = DiscordAdapter.disconnect

    async def _wrapped_disconnect(self, *args, **kwargs):
        try:
            from . import coder_event_bus
            from .coder_sessions import get_global_sessions, set_global_sessions

            coder_event_bus.unregister_handler(self.on_coder_event)
            if get_global_sessions() is getattr(self, "_coder_sessions", None):
                set_global_sessions(None)
        except Exception:
            pass
        return await _orig_disconnect(self, *args, **kwargs)

    DiscordAdapter.disconnect = _wrapped_disconnect

    # 5. _handle_message wrap: route messages landing in a coder-bound thread
    #    to that coder (cancel / follow-up) instead of the main Hermes brain.
    #    on_message is the sole caller and its channel filters run before it
    #    reaches here, so ordering is preserved.
    _orig_handle_message = DiscordAdapter._handle_message

    async def _wrapped_handle_message(self, message):
        sessions = getattr(self, "_coder_sessions", None)
        if sessions is not None and isinstance(message.channel, discord.Thread):
            _cid = sessions.get_coder_by_thread(str(message.channel.id))
            if _cid:
                from .delegate_background import is_cancel_command
                if is_cancel_command(message.content):
                    await self._cancel_coder_run(_cid, message.channel)
                    return
                sessions.touch(_cid)
                await self._handle_coder_followup(
                    _cid, message.content, message.channel
                )
                return
        return await _orig_handle_message(self, message)

    DiscordAdapter._handle_message = _wrapped_handle_message

    # 6. _register_slash_commands POST-wrap: add the /code slash command to the
    #    command tree (before the post-connect tree.sync picks it up).
    _orig_register_slash = DiscordAdapter._register_slash_commands

    def _wrapped_register_slash(self, *args, **kwargs):
        result = _orig_register_slash(self, *args, **kwargs)
        _add_code_command(self)
        return result

    DiscordAdapter._register_slash_commands = _wrapped_register_slash

    DiscordAdapter._subagent_coder_overlay_installed = True
    logger.info("subagent_coder: DiscordAdapter coder overlay installed")

    # 7. Retrofit a live adapter if discovery ran after it connected.
    _retrofit_live_discord_adapter()


def _retrofit_live_discord_adapter() -> None:
    """Wire a DiscordAdapter that was already created/connected before the
    overlay installed (gateway discovers plugins lazily, often after connect).

    The class wraps don't help an instance whose ``__init__`` /
    ``_run_post_connect_initialization`` / ``_register_slash_commands`` already
    ran, so we reach the live instance via the gateway runner weakref and apply
    the coder state, ``/code`` command, progress flusher/bus, and a tree resync
    directly on its event loop.
    """
    import asyncio

    gw = sys.modules.get("gateway.run")
    ref = getattr(gw, "_gateway_runner_ref", None) if gw is not None else None
    runner = ref() if callable(ref) else None
    if runner is None:
        return
    adapter = None
    for a in getattr(runner, "adapters", {}).values():
        if type(a).__name__ == "DiscordAdapter":
            adapter = a
            break
    if adapter is None:
        return
    if getattr(adapter, "_subagent_coder_retrofitted", False):
        return
    adapter._subagent_coder_retrofitted = True

    _ensure_coder_state(adapter)

    client = getattr(adapter, "_client", None)
    loop = getattr(client, "loop", None) if client is not None else None
    if loop is None or not getattr(loop, "is_running", lambda: False)():
        # Not connected yet — the normal connect-path wraps will wire it.
        return

    async def _retro() -> None:
        try:
            _add_code_command(adapter)
        except Exception:
            logger.debug("subagent_coder: retrofit add /code failed", exc_info=True)
        try:
            _start_coder_progress(adapter)
        except Exception:
            logger.debug("subagent_coder: retrofit start progress failed", exc_info=True)
        try:
            await adapter._client.tree.sync()
            logger.info(
                "subagent_coder: retrofitted live DiscordAdapter (/code added + synced)"
            )
        except Exception:
            logger.debug("subagent_coder: retrofit tree.sync failed", exc_info=True)

    try:
        asyncio.run_coroutine_threadsafe(_retro(), loop)
    except Exception:
        logger.debug("subagent_coder: retrofit schedule failed", exc_info=True)
