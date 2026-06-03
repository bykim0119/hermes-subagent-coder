"""_install_coder_child_wraps() 단위 검증.

stock delegate_task에는 override_provider/subagent_id_override 파라미터가 없으므로,
코더 child의 provider/api_mode/subagent_id는 _coder_child_ctx ContextVar + 두 wrap
(_build_child_agent / _build_child_progress_callback)으로 런타임 주입된다.
여기서는 stock 원본을 stub으로 갈아끼워 wrap이 주입하는 값만 격리 검증한다.
"""
from unittest.mock import MagicMock

import tools.delegate_tool as dt
from subagent_coder import _install_coder_child_wraps
from subagent_coder.delegate_background import _coder_child_ctx

_CTX = {
    "subagent_id": "coder-abc123",
    "provider": "codex-exec",
    "api_mode": "chat_completions",
}


def _install_with_stubs(monkeypatch, child_stub, cb_stub):
    """원본을 stub으로 교체한 뒤 wrap을 (재)설치. monkeypatch가 teardown에서
    원본·sentinel을 복원하므로 다른 테스트와 격리된다."""
    monkeypatch.setattr(dt, "_build_child_agent", child_stub)
    monkeypatch.setattr(dt, "_build_child_progress_callback", cb_stub)
    monkeypatch.setattr(dt, "_subagent_coder_child_wrapped", False, raising=False)
    _install_coder_child_wraps()


def test_build_child_agent_injects_overrides_and_pins_subagent_id(monkeypatch):
    captured = {}
    child = MagicMock()

    def fake_build_child_agent(*args, **kwargs):
        captured.update(kwargs)
        return child

    _install_with_stubs(monkeypatch, fake_build_child_agent, lambda *a, **k: None)

    token = _coder_child_ctx.set(dict(_CTX))
    try:
        result = dt._build_child_agent(
            task_index=0,
            goal="g",
            override_provider="parent-provider",
            override_api_mode="codex_responses",
        )
    finally:
        _coder_child_ctx.reset(token)

    # PRE: stock가 넘긴 parent 값을 codex-exec / chat_completions로 덮어씀
    assert captured["override_provider"] == "codex-exec"
    assert captured["override_api_mode"] == "chat_completions"
    # POST: registry/interrupt/Discord 라우팅이 coder_run_id로 키잉되도록 고정
    assert result is child
    assert child._subagent_id == "coder-abc123"


def test_progress_callback_replaces_subagent_id(monkeypatch):
    captured = {}

    def fake_cb(*args, **kwargs):
        captured.update(kwargs)
        return "cb"

    _install_with_stubs(monkeypatch, lambda *a, **k: MagicMock(), fake_cb)

    token = _coder_child_ctx.set(dict(_CTX))
    try:
        dt._build_child_progress_callback(0, "g", None, subagent_id="sa-0-deadbeef")
    finally:
        _coder_child_ctx.reset(token)

    assert captured["subagent_id"] == "coder-abc123"


def test_no_ctx_is_passthrough(monkeypatch):
    """ContextVar 미설정(비코더 위임)이면 wrap이 아무것도 바꾸지 않는다."""
    captured_agent = {}
    captured_cb = {}

    def fake_build_child_agent(*args, **kwargs):
        captured_agent.update(kwargs)
        return MagicMock()

    def fake_cb(*args, **kwargs):
        captured_cb.update(kwargs)
        return "cb"

    _install_with_stubs(monkeypatch, fake_build_child_agent, fake_cb)

    # _coder_child_ctx default = None
    dt._build_child_agent(task_index=0, override_provider="parent-provider")
    dt._build_child_progress_callback(0, "g", None, subagent_id="sa-0-orig")

    assert captured_agent["override_provider"] == "parent-provider"  # 그대로
    assert captured_cb["subagent_id"] == "sa-0-orig"  # 그대로


def test_install_is_idempotent(monkeypatch):
    _install_with_stubs(monkeypatch, lambda *a, **k: MagicMock(), lambda *a, **k: None)
    first = dt._build_child_agent
    _install_coder_child_wraps()  # 재호출 — sentinel True라 재wrap 안 함
    assert dt._build_child_agent is first
