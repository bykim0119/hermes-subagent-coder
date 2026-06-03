"""Coder session manager: coder_run_id ↔ Discord thread mapping.

Tracks active coder runs so that:
  - subagent_progress events for coder_run_id X route to thread_id Y
  - follow-up messages in thread_id Y route back to coder_run_id X
  - idle sessions are evicted after ``idle_timeout_seconds``
  - concurrent active runs cap at ``max_concurrent``

In-memory only (V1). Restart-survivable persistence is V2.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class _Session:
    coder_run_id: str
    thread_id: str
    parent_channel_id: str
    # Codex CLI session UUID emitted by ``thread.started`` events. Captured on
    # first spawn so follow-up turns can use ``codex exec resume <uuid>`` to
    # re-enter the same conversation context instead of restarting cold.
    codex_session_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)


class CoderSessionManager:
    def __init__(self, idle_timeout_seconds: int = 7200, max_concurrent: int = 3):
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_concurrent = max_concurrent
        self._by_coder: Dict[str, _Session] = {}
        self._by_thread: Dict[str, str] = {}
        self._lock = threading.Lock()

    def bind(
        self, coder_run_id: str, thread_id: str, parent_channel_id: str
    ) -> None:
        with self._lock:
            self._evict_idle_locked()
            if len(self._by_coder) >= self.max_concurrent:
                raise ValueError(
                    f"max_concurrent ({self.max_concurrent}) coder sessions active. "
                    "Wait for one to finish or cancel an existing thread."
                )
            sess = _Session(
                coder_run_id=coder_run_id,
                thread_id=thread_id,
                parent_channel_id=parent_channel_id,
            )
            self._by_coder[coder_run_id] = sess
            self._by_thread[thread_id] = coder_run_id

    def get_thread(self, coder_run_id: str) -> Optional[str]:
        with self._lock:
            sess = self._by_coder.get(coder_run_id)
            return sess.thread_id if sess else None

    def get_coder_by_thread(self, thread_id: str) -> Optional[str]:
        with self._lock:
            return self._by_thread.get(thread_id)

    def touch(self, coder_run_id: str) -> None:
        with self._lock:
            sess = self._by_coder.get(coder_run_id)
            if sess:
                sess.last_activity_at = time.time()

    def unbind(self, coder_run_id: str) -> None:
        with self._lock:
            sess = self._by_coder.pop(coder_run_id, None)
            if sess:
                self._by_thread.pop(sess.thread_id, None)

    def set_codex_session_id(self, coder_run_id: str, session_id: str) -> None:
        with self._lock:
            sess = self._by_coder.get(coder_run_id)
            if sess:
                sess.codex_session_id = session_id

    def get_codex_session_id(self, coder_run_id: str) -> Optional[str]:
        with self._lock:
            sess = self._by_coder.get(coder_run_id)
            return sess.codex_session_id if sess else None

    def tick(self) -> int:
        """Housekeeping: evict idle sessions. Returns count evicted."""
        with self._lock:
            return self._evict_idle_locked()

    def active_count(self) -> int:
        with self._lock:
            return len(self._by_coder)

    def _evict_idle_locked(self) -> int:
        now = time.time()
        evicted = []
        for cid, sess in list(self._by_coder.items()):
            if now - sess.last_activity_at > self.idle_timeout_seconds:
                evicted.append(cid)
        for cid in evicted:
            sess = self._by_coder.pop(cid, None)
            if sess:
                self._by_thread.pop(sess.thread_id, None)
        return len(evicted)


# Module-level pointer to the gateway's active CoderSessionManager. The Discord
# adapter publishes its instance here at startup so the coder sink (which lives
# in tools/delegate_tool.py, outside the gateway package boundary) can resolve
# the manager without a parameter chain or circular import.
_GLOBAL_SESSIONS: Optional["CoderSessionManager"] = None


def set_global_sessions(sessions: Optional["CoderSessionManager"]) -> None:
    global _GLOBAL_SESSIONS
    _GLOBAL_SESSIONS = sessions


def get_global_sessions() -> Optional["CoderSessionManager"]:
    return _GLOBAL_SESSIONS
