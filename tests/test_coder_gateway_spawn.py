"""S3 gateway coder_spawn_callback wraps 검증.

stock gateway/run.py를 안 건드리고, _run_agent wrap(ContextVar 세팅) +
run_conversation wrap(agent에 설치)로 per-turn coder_spawn_callback을 복원했는지
확인. _run_agent는 fake GatewayRunner 모듈로 POC 한다.
"""
import asyncio
import sys
import types

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from subagent_coder import (
    _build_coder_spawn_callback,
    _coder_spawn_cb_ctx,
    _install_gateway_coder_spawn_wraps,
)


def _make_source(platform="discord", chat_id="C1", thread_id="T9"):
    src = MagicMock()
    src.platform = platform
    src.chat_id = chat_id
    src.thread_id = thread_id
    return src


# --- _build_coder_spawn_callback ---------------------------------------------

def test_callback_schedules_create_coder_thread():
    adapter = MagicMock()  # create_coder_thread 존재
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    runner._is_session_run_current.return_value = True
    src = _make_source()

    cb = _build_coder_spawn_callback(runner, src, "skey", 3, loop="LOOP")

    with patch("asyncio.run_coroutine_threadsafe") as mock_sched:
        cb("coder-1", "do the thing")

    assert mock_sched.called
    # create_coder_thread가 올바른 인자로 호출됨
    adapter.create_coder_thread.assert_called_once_with(
        coder_run_id="coder-1", goal="do the thing",
        chat_id="C1", parent_thread_id="T9",
    )
    # 캡처된 loop 사용
    assert mock_sched.call_args.args[1] == "LOOP"


def test_callback_noop_when_adapter_missing():
    runner = MagicMock()
    runner.adapters = {}  # 해당 platform adapter 없음
    runner._is_session_run_current.return_value = True
    cb = _build_coder_spawn_callback(runner, _make_source(), "skey", 1, loop="LOOP")
    with patch("asyncio.run_coroutine_threadsafe") as mock_sched:
        cb("coder-x", "g")
    assert not mock_sched.called


def test_callback_noop_when_run_stale():
    adapter = MagicMock()
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    runner._is_session_run_current.return_value = False  # stale
    cb = _build_coder_spawn_callback(runner, _make_source(), "skey", 5, loop="LOOP")
    with patch("asyncio.run_coroutine_threadsafe") as mock_sched:
        cb("coder-x", "g")
    assert not mock_sched.called


def test_callback_noop_when_adapter_lacks_method():
    adapter = MagicMock(spec=[])  # create_coder_thread 없음
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    runner._is_session_run_current.return_value = True
    cb = _build_coder_spawn_callback(runner, _make_source(), "skey", 1, loop="LOOP")
    with patch("asyncio.run_coroutine_threadsafe") as mock_sched:
        cb("coder-x", "g")
    assert not mock_sched.called


def test_callback_run_current_when_no_generation():
    """run_generation None이면 항상 current (stock 동작)."""
    adapter = MagicMock()
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    cb = _build_coder_spawn_callback(runner, _make_source(), None, None, loop="LOOP")
    with patch("asyncio.run_coroutine_threadsafe") as mock_sched:
        cb("coder-x", "g")
    assert mock_sched.called
    runner._is_session_run_current.assert_not_called()


# --- run_conversation wrap ---------------------------------------------------

def test_run_conversation_wrap_installs_callback_from_ctx(monkeypatch):
    captured = {}

    def fake_run_conversation(self, *args, **kwargs):
        captured["cb"] = self.coder_spawn_callback
        return "ok"

    monkeypatch.setattr(AIAgent, "run_conversation", fake_run_conversation)
    monkeypatch.setattr(AIAgent, "_subagent_coder_run_conversation_wrapped", False, raising=False)
    # CLI 모드 시뮬: gateway.run 미로드 → run_conversation만 wrap
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    _install_gateway_coder_spawn_wraps()

    agent = MagicMock()
    sentinel_cb = lambda rid, goal: None
    token = _coder_spawn_cb_ctx.set(sentinel_cb)
    try:
        result = AIAgent.run_conversation(agent, "msg")
    finally:
        _coder_spawn_cb_ctx.reset(token)

    assert result == "ok"
    assert captured["cb"] is sentinel_cb
    assert agent.coder_spawn_callback is sentinel_cb


def test_run_conversation_wrap_noop_without_ctx(monkeypatch):
    def fake_run_conversation(self, *args, **kwargs):
        return "ok"

    monkeypatch.setattr(AIAgent, "run_conversation", fake_run_conversation)
    monkeypatch.setattr(AIAgent, "_subagent_coder_run_conversation_wrapped", False, raising=False)
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    _install_gateway_coder_spawn_wraps()

    agent = MagicMock(spec=["run_conversation"])
    # ctx 미설정 → coder_spawn_callback 미설치
    assert _coder_spawn_cb_ctx.get() is None
    AIAgent.run_conversation(agent, "msg")
    assert not hasattr(agent, "coder_spawn_callback")


# --- gateway _run_agent wrap (fake module POC) -------------------------------

def test_run_agent_wrap_sets_ctx_during_turn(monkeypatch):
    """fake gateway.run.GatewayRunner로 _run_agent wrap이 turn 동안
    _coder_spawn_cb_ctx를 세팅하고, 끝나면 reset하는지 POC."""
    seen = {}

    class FakeGatewayRunner:
        def __init__(self):
            self.adapters = {"discord": MagicMock()}

        def _is_session_run_current(self, session_key, gen):
            return True

        # 실제 시그니처와 동일하게(_run_agent_sig.bind 대상)
        async def _run_agent(
            self, message, context_prompt, history, source, session_id,
            session_key=None, run_generation=None, _interrupt_depth=0,
            event_message_id=None, channel_prompt=None,
        ):
            seen["cb"] = _coder_spawn_cb_ctx.get()
            return {"final_response": "hi"}

    fake_mod = types.ModuleType("gateway.run")
    fake_mod.GatewayRunner = FakeGatewayRunner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_mod)
    # run_conversation wrap sentinel은 영향 없게 리셋
    monkeypatch.setattr(AIAgent, "_subagent_coder_run_conversation_wrapped", False, raising=False)

    _install_gateway_coder_spawn_wraps()
    assert getattr(FakeGatewayRunner, "_subagent_coder_run_agent_wrapped", False)

    runner = FakeGatewayRunner()
    src = _make_source()

    async def _drive():
        return await runner._run_agent(
            "msg", "ctx", [], src, "sess-1",
            session_key="skey", run_generation=2,
        )

    assert _coder_spawn_cb_ctx.get() is None  # 전
    result = asyncio.run(_drive())
    assert result["final_response"] == "hi"
    assert _coder_spawn_cb_ctx.get() is None  # 후(reset)

    # turn 동안 콜백이 세팅됐고 호출 가능
    assert callable(seen["cb"])
    # 세팅된 콜백이 fake adapter.create_coder_thread를 스케줄
    with patch("asyncio.run_coroutine_threadsafe") as mock_sched:
        seen["cb"]("coder-poc", "goal")
    assert mock_sched.called
    runner.adapters["discord"].create_coder_thread.assert_called_once()
