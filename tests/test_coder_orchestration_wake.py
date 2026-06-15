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


# --- 완료 웨이크 -------------------------------------------------------------

from subagent_coder import coder_orchestration as orch


def _install_fake_gateway(monkeypatch, adapter):
    """gateway.run._gateway_runner_ref가 adapter를 가진 runner를 가리키게 한다."""
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    fake_mod = types.ModuleType("gateway.run")
    fake_mod._gateway_runner_ref = lambda: runner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_mod)
    return runner


def _seed_orch(cid, status, **extra):
    db._register_coder_run(cid, "parent", extra.pop("goal", "goal"))
    src = MagicMock()
    src.platform = "discord"
    src.chat_id = "C1"
    src.thread_id = "T1"
    db.record_main_routing(cid, src, loop="LOOP")
    rec = db._CODER_RUN_REGISTRY[cid]
    rec["status"] = status
    rec.update(extra)
    return rec, src


def test_wake_success_injects_message(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    _seed_orch("coder-w1", "completed", result="built the thing")

    with patch("asyncio.run_coroutine_threadsafe") as sched:
        orch.notify_main_on_completion("coder-w1")

    assert sched.called
    # handle_message coroutine + 캡처된 loop
    assert sched.call_args.args[1] == "LOOP"
    synth = adapter.handle_message.call_args.args[0]
    assert synth.internal is True
    assert "완료" in synth.text and "built the thing" in synth.text


def test_wake_failure_includes_error_and_log(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    rec, _ = _seed_orch("coder-w2", "failed", error="boom")
    rec["log"].append({"event": "agent.message", "data": {"text": "last line"}})

    with patch("asyncio.run_coroutine_threadsafe"):
        orch.notify_main_on_completion("coder-w2")

    synth = adapter.handle_message.call_args.args[0]
    assert "실패" in synth.text and "boom" in synth.text and "last line" in synth.text


def test_wake_cancelled(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    _seed_orch("coder-w3", "cancelled")

    with patch("asyncio.run_coroutine_threadsafe"):
        orch.notify_main_on_completion("coder-w3")

    synth = adapter.handle_message.call_args.args[0]
    assert "취소" in synth.text


def test_wake_dedup_single_injection(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    _seed_orch("coder-w4", "completed", result="r")

    with patch("asyncio.run_coroutine_threadsafe") as sched:
        orch.notify_main_on_completion("coder-w4")
        orch.notify_main_on_completion("coder-w4")   # 두 번째는 claim 실패

    assert sched.call_count == 1


def test_wake_skips_code_run(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    db._register_coder_run("coder-slash", "slash:/code:1", "gs")
    db._CODER_RUN_REGISTRY["coder-slash"]["status"] = "completed"

    with patch("asyncio.run_coroutine_threadsafe") as sched:
        orch.notify_main_on_completion("coder-slash")

    assert not sched.called   # 라우팅 없음 → claim None → no-op
