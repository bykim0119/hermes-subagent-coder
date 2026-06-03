"""Coder event bus — fan out coder NDJSON events to registered platform adapters.

The coder sink (``tools/delegate_tool._build_coder_progress_sink``) runs inside
a background daemon thread and needs to push events into the asyncio loop where
the platform adapter (Discord, Slack, …) lives. Pre-unification this was done
indirectly through ``parent_agent.tool_progress_callback``, but that callback
was wired as a turn-local closure — it didn't exist for follow-up turns spawned
outside a parent agent run.

This bus removes the parent_agent dependency: adapters register their async
``on_coder_event`` handler along with their asyncio loop at startup, and the
sink calls :func:`dispatch` which schedules the handler on each registered
loop via ``run_coroutine_threadsafe``.

Thread-safe by design — registration/dispatch happen from different threads
(adapter init runs on the gateway event loop; sink fires from the codex
subprocess reader thread).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, List, Tuple

logger = logging.getLogger(__name__)

CoderHandler = Callable[[str, dict], Awaitable[None]]

_HANDLERS: List[Tuple[CoderHandler, asyncio.AbstractEventLoop]] = []
_LOCK = threading.Lock()


def register_handler(handler: CoderHandler, loop: asyncio.AbstractEventLoop) -> None:
    """Register an adapter's coder event handler bound to its asyncio loop.

    Idempotent for the same (handler, loop) pair — re-registering is a no-op.
    """
    with _LOCK:
        for h, l in _HANDLERS:
            if h is handler and l is loop:
                return
        _HANDLERS.append((handler, loop))


def unregister_handler(handler: CoderHandler) -> None:
    """Remove all entries for the given handler (any loop)."""
    with _LOCK:
        _HANDLERS[:] = [(h, l) for (h, l) in _HANDLERS if h is not handler]


def dispatch(coder_run_id: str, payload: dict) -> None:
    """Fan out a coder event to every registered handler.

    Called from the sink (any thread). Each handler is scheduled on its own
    registered loop via ``run_coroutine_threadsafe`` so we never block the
    caller. Handler exceptions are logged but never raised — one broken
    adapter must not stop events from reaching others.
    """
    with _LOCK:
        snapshot = list(_HANDLERS)
    for handler, loop in snapshot:
        try:
            asyncio.run_coroutine_threadsafe(handler(coder_run_id, payload), loop)
        except Exception:
            logger.debug("coder_event_bus dispatch failed", exc_info=True)


def _reset_for_tests() -> None:
    """Test-only helper to clear all registrations."""
    with _LOCK:
        _HANDLERS.clear()
