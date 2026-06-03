"""Format coder subagent progress events into emoji-prefixed Discord messages."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Awaitable, Callable, Optional

MAX_CHUNK_CHARS = 3500


def format_event(event: dict) -> Optional[str]:
    """Convert a subagent_progress event dict into a thread message string.

    Accepts both the hermes-internal shape (``{"event": "tool_call", ...}``)
    and the Codex CLI NDJSON shape (``{"event": "item.completed",
    "data": {...}}``) so the formatter is the single normalisation point.

    Returns None for events that should not be rendered.
    Caller is responsible for debounce / batching.
    """
    et = event.get("event")
    # Codex CLI events ------------------------------------------------------
    # Dotted event names (e.g. "item.completed") are the obvious marker;
    # "error" and "raw" are flat but still Codex when shaped with a ``data``
    # dict — distinguish from the hermes-flat shape that uses top-level keys.
    _is_codex = bool(et) and (
        "." in et or (et in ("error", "raw") and isinstance(event.get("data"), dict))
    )
    if _is_codex:
        data = event.get("data") or {}
        if et == "thread.started" or et == "turn.started":
            return None
        if et == "item.started":
            # Skip start markers — item.completed lands shortly with the full
            # outcome (exit code, output) and rendering both clutters the
            # thread with duplicates of every command.
            return None
        if et == "item.completed":
            item = data.get("item") or {}
            itype = item.get("type") or ""
            text = item.get("text") or ""
            if itype == "agent_message":
                return _cap(text) if text else None
            if itype in ("reasoning",):
                return f"💭 {_cap(text)}" if text else None
            if itype in ("command_execution", "local_shell_call", "function_call"):
                cmd = item.get("command") or item.get("name") or text or itype
                # Codex wraps almost everything in ``/bin/bash -lc "..."`` —
                # strip the wrapper so the visible line is the actual command
                # the model asked for, not the shell invocation harness.
                cmd = _strip_bash_wrapper(cmd)
                exit_code = item.get("exit_code")
                if isinstance(exit_code, int) and exit_code != 0:
                    return f"▶️ {_cap(cmd)}  ⚠️ exit {exit_code}"
                return f"▶️ {_cap(cmd)}"
            if itype in ("file_change",):
                path = item.get("path") or "?"
                return f"✏️ {path}"
            label = itype or "item"
            return f"📦 {label}" if not text else f"📦 {label}: {_cap(text)}"
        if et == "turn.completed":
            usage = data.get("usage") or {}
            out = usage.get("output_tokens")
            if out is not None:
                return f"✅ 완료 ({out} out tokens)"
            return "✅ 완료"
        if et == "error":
            stderr = data.get("stderr") or data.get("message") or ""
            rc = data.get("returncode")
            head = f"❌ codex exit {rc}" if rc is not None else "❌ codex error"
            return f"{head}\n{_cap(stderr)}" if stderr else head
        if et == "raw":
            return _cap(data.get("text", ""))
        return None
    # Hermes-internal events -----------------------------------------------
    if et == "tool_call":
        tool = event.get("tool", "")
        if tool == "read_file":
            return f"🔧 reading {event.get('path', '?')}"
        if tool == "edit_file":
            path = event.get("path", "?")
            added = event.get("added")
            removed = event.get("removed")
            if added is not None or removed is not None:
                return f"✏️ editing {path} (+{added or 0} -{removed or 0})"
            return f"✏️ editing {path}"
        if tool == "terminal":
            cmd = event.get("command", "")
            return f"▶️ $ {cmd}"
        return f"🔧 {tool}"
    if et == "text_delta":
        text = event.get("text", "")
        return _cap(text)
    if et == "turn_complete":
        summary = event.get("summary", "")
        return f"✅ 완료 — {summary}" if summary else "✅ 완료"
    if et == "error":
        msg = event.get("message", "(unknown error)")
        return f"❌ {_cap(msg)}"
    if et == "warning":
        return f"⚠️ {_cap(event.get('message', ''))}"
    if et == "plan":
        return f"📌 plan: {_cap(event.get('text', ''))}"
    return None


def _cap(text: str) -> str:
    if len(text) <= MAX_CHUNK_CHARS:
        return text
    return text[:MAX_CHUNK_CHARS] + "…[truncated]"


def _strip_bash_wrapper(cmd: str) -> str:
    """Codex usually wraps shell invocations as ``/bin/bash -lc "<actual>"``;
    surface the actual command so the rendered line matches user intent."""
    if not cmd:
        return cmd
    s = cmd.strip()
    for prefix in ("/bin/bash -lc ", "bash -lc ", "/bin/sh -c ", "sh -c "):
        if s.startswith(prefix):
            inner = s[len(prefix):].strip()
            # Strip surrounding quotes (single or double) — keep inner as-is.
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in ("'", '"'):
                inner = inner[1:-1]
            return inner
    return cmd


class DebouncedFlusher:
    """Collect short messages per thread and flush every ``interval_ms``.

    Caller schedules events via :meth:`add`. A background asyncio task
    flushes accumulated buffers by invoking the publish coroutine.
    """

    def __init__(
        self,
        interval_ms: int = 250,
        publish: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        self.interval = interval_ms / 1000.0
        self.publish = publish
        self._buffers: dict[str, list[str]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def add(self, thread_id: str, text: str) -> None:
        async with self._lock:
            self._buffers[thread_id].append(text)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            async with self._lock:
                snapshots = {
                    tid: "\n".join(parts)
                    for tid, parts in self._buffers.items()
                    if parts
                }
                self._buffers.clear()
            for tid, body in snapshots.items():
                if self.publish:
                    try:
                        await self.publish(tid, body)
                    except Exception:
                        pass
