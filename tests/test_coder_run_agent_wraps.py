"""S2 run_agent.py wraps 검증 — stock run_agent.py를 안 건드리고 코더 wiring을
register(ctx)의 monkey-patch로 복원했는지 확인.

대상:
- _install_sequential_dispatch_wrap: _execute_tool_calls_sequential가 돌 때
  _dispatch_parent_agent ContextVar = self → registry handler가 폴백으로 읽음.
- delegate_task_background registry handler: parent_agent 미전달 시 ContextVar 폴백.
- _install_codex_exec_client_factory_wrap: provider==codex-exec면 CodexExecFacade 반환.
- _install_coder_spawn_callback_slot: AIAgent에 클래스 기본값.
"""
import json
from unittest.mock import MagicMock, patch

import run_agent
from run_agent import AIAgent
from subagent_coder.delegate_background import _dispatch_parent_agent


# --- sequential dispatch wrap + handler 폴백 ---------------------------------

def test_sequential_wrap_sets_parent_agent_contextvar(monkeypatch):
    from subagent_coder import _install_sequential_dispatch_wrap

    seen = {}

    def fake_sequential(self, *args, **kwargs):
        seen["parent"] = _dispatch_parent_agent.get()
        return "done"

    monkeypatch.setattr(AIAgent, "_execute_tool_calls_sequential", fake_sequential)
    monkeypatch.setattr(AIAgent, "_subagent_coder_sequential_wrapped", False, raising=False)
    _install_sequential_dispatch_wrap()

    agent = MagicMock()
    # ContextVar는 호출 전 None, 호출 중 self, 호출 후 다시 None(reset)
    assert _dispatch_parent_agent.get() is None
    result = AIAgent._execute_tool_calls_sequential(agent, None, [], "task-1")
    assert result == "done"
    assert seen["parent"] is agent
    assert _dispatch_parent_agent.get() is None  # finally reset


def test_registry_handler_falls_back_to_contextvar(monkeypatch):
    """delegate_task_background이 registry 경로로 dispatch될 때(parent_agent 없음)
    _dispatch_parent_agent ContextVar에서 parent_agent를 끌어온다."""
    from tools.registry import registry

    agent = MagicMock()
    agent.task_id = "parent-task"

    with patch(
        "subagent_coder.delegate_background._spawn_detached_coder"
    ) as mock_spawn, patch(
        "subagent_coder.coder_config.check_codex_auth", return_value=None
    ):
        token = _dispatch_parent_agent.set(agent)
        try:
            raw = registry.dispatch("delegate_task_background", {"goal": "fix bug"})
        finally:
            _dispatch_parent_agent.reset(token)

    result = json.loads(raw)
    assert result["status"] == "spawned"
    assert result["coder_run_id"].startswith("coder-")
    # parent_agent이 ContextVar로 주입돼 spawn까지 도달
    assert mock_spawn.called
    assert mock_spawn.call_args.kwargs["parent_agent"] is agent


def test_registry_handler_no_agent_returns_error(monkeypatch):
    """ContextVar도 없고 kw에도 parent_agent 없으면 구조화된 에러."""
    from tools.registry import registry

    # ContextVar default None 상태
    assert _dispatch_parent_agent.get() is None
    raw = registry.dispatch("delegate_task_background", {"goal": "x"})
    result = json.loads(raw)
    assert "error" in result


# --- codex-exec client factory wrap ------------------------------------------

def test_client_factory_wrap_returns_facade_for_codex_exec(monkeypatch):
    from subagent_coder import _install_codex_exec_client_factory_wrap

    orig_called = {"n": 0}

    def fake_orig(self, client_kwargs, *, reason, shared):
        orig_called["n"] += 1
        return "http-client"

    monkeypatch.setattr(AIAgent, "_create_openai_client", fake_orig)
    monkeypatch.setattr(AIAgent, "_subagent_coder_client_factory_wrapped", False, raising=False)
    _install_codex_exec_client_factory_wrap()

    fake_facade = object()
    agent = MagicMock()
    agent.provider = "codex-exec"
    agent._subagent_id = "coder-xyz"
    agent._client_log_context.return_value = "[ctx]"

    with patch(
        "subagent_coder.codex_exec_client.CodexExecFacade",
        return_value=fake_facade,
    ) as mock_facade, patch(
        "hermes_cli.auth.resolve_external_process_provider_credentials",
        return_value={"command": "codex", "args": ["exec"]},
    ):
        result = AIAgent._create_openai_client(
            agent, {"api_key": "k", "base_url": "codex-exec://local"},
            reason="test", shared=False,
        )

    assert result is fake_facade
    assert orig_called["n"] == 0  # codex-exec는 orig 미호출
    assert mock_facade.call_args.kwargs["subagent_id"] == "coder-xyz"
    assert mock_facade.call_args.kwargs["command"] == "codex"


def test_client_factory_wrap_passthrough_for_other_providers(monkeypatch):
    from subagent_coder import _install_codex_exec_client_factory_wrap

    def fake_orig(self, client_kwargs, *, reason, shared):
        return "http-client"

    monkeypatch.setattr(AIAgent, "_create_openai_client", fake_orig)
    monkeypatch.setattr(AIAgent, "_subagent_coder_client_factory_wrapped", False, raising=False)
    _install_codex_exec_client_factory_wrap()

    agent = MagicMock()
    agent.provider = "openai"
    result = AIAgent._create_openai_client(
        agent, {"api_key": "k", "base_url": "https://api.openai.com/v1"},
        reason="test", shared=True,
    )
    assert result == "http-client"  # 비-codex는 stock 경로


# --- coder_spawn_callback slot -----------------------------------------------

def test_coder_spawn_callback_slot_installed():
    from subagent_coder import _install_coder_spawn_callback_slot

    _install_coder_spawn_callback_slot()
    # 클래스 기본값 None — getattr이 항상 resolvable
    agent = MagicMock(spec=[])  # 인스턴스 attr 없음
    assert getattr(AIAgent, "coder_spawn_callback", "MISSING") is None
