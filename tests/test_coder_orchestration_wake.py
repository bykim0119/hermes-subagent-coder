"""코더 오케스트레이션 — 로그 캡처 + 완료 웨이크 검증."""
import sys
import types
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from subagent_coder import delegate_background as db


@pytest.fixture(autouse=True)
def _clean_registry():
    db._CODER_RUN_REGISTRY.clear()
    yield
    db._CODER_RUN_REGISTRY.clear()


def _evt(name, data=None):
    e = MagicMock()
    e.event = name
    e.data = data or {}
    return e


def test_register_seeds_log_deque():
    db._register_coder_run("coder-log1", "parent", "goal")
    rec = db._CODER_RUN_REGISTRY["coder-log1"]
    assert isinstance(rec["log"], deque)
    assert rec["log"].maxlen == db._LOG_MAXLEN


def test_sink_captures_events_into_log():
    db._register_coder_run("coder-log2", "parent", "goal")
    sink = db._build_coder_progress_sink("coder-log2")
    sink(_evt("agent.thinking", {"text": "hi"}))
    sink(_evt("agent.message", {"text": "done"}))
    rec = db._CODER_RUN_REGISTRY["coder-log2"]
    captured = list(rec["log"])
    assert captured == [
        {"event": "agent.thinking", "data": {"text": "hi"}},
        {"event": "agent.message", "data": {"text": "done"}},
    ]


def test_spawn_callback_records_routing():
    from subagent_coder import _build_coder_spawn_callback

    db._register_coder_run("coder-cb", "parent", "goal")
    adapter = MagicMock()
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    runner._is_session_run_current.return_value = True
    src = MagicMock()
    src.platform = "discord"
    src.chat_id = "C1"
    src.thread_id = "T1"

    cb = _build_coder_spawn_callback(runner, src, "skey", 1, loop="LOOP")
    with patch("asyncio.run_coroutine_threadsafe"):
        cb("coder-cb", "goal")

    rec = db._CODER_RUN_REGISTRY["coder-cb"]
    assert rec["main_source"] is src
    assert rec["main_loop"] == "LOOP"
