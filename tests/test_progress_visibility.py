"""진행 가시성 — ctx 확장·relay·codex skip 검증."""
from unittest.mock import MagicMock, patch

from subagent_coder import delegate_background as db
from subagent_coder.coder_roles import get_role


def _capture_ctx_during_spawn(role_name):
    """역할 스폰 중 delegate_task 호출 시점의 _coder_child_ctx 값을 캡처."""
    db._CODER_RUN_REGISTRY.clear()
    db._register_coder_run("rid", "parent", "goal", role=role_name)
    captured = {}

    def fake_delegate_task(**kwargs):
        captured["ctx"] = db._coder_child_ctx.get()
        return "done"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task), \
         patch("subagent_coder.codex_exec_client.register_coder_sink"), \
         patch("subagent_coder.codex_exec_client.unregister_coder_sink"), \
         patch("subagent_coder.coder_orchestration.notify_main_on_completion"):
        db._spawn_detached_coder(MagicMock(), "goal", "", "rid", get_role(role_name))
        import time; time.sleep(0.2)
    db._CODER_RUN_REGISTRY.clear()
    return captured["ctx"]


def test_ctx_set_for_noncodex_with_use_codex_false():
    ctx = _capture_ctx_during_spawn("reviewer")
    assert ctx is not None                  # 비-codex도 ctx set(번호표)
    assert ctx["subagent_id"] == "rid"
    assert ctx["use_codex"] is False
    assert ctx["provider"] is None          # codex override 없음


def test_ctx_set_for_codex_with_use_codex_true():
    ctx = _capture_ctx_during_spawn("coder")
    assert ctx is not None
    assert ctx["use_codex"] is True
    assert ctx["provider"] == "codex-exec"


def test_relay_helper_dispatches_tool_call():
    """비-codex 자식 도구 호출을 coder_event_bus로 tool_call 형식으로 relay."""
    import subagent_coder as sc
    from subagent_coder import coder_event_bus
    captured = []
    with patch.object(coder_event_bus, "dispatch",
                      lambda rid, p: captured.append((rid, p))):
        sc._relay_child_tool_call("rid", "scan_pii", {"path": "x.txt"}, None)
    assert captured, "도구 호출이 bus로 relay되어야 함"
    rid, payload = captured[0]
    assert rid == "rid"
    assert payload["event"] == "tool_call"
    assert payload["tool"] == "scan_pii"
    assert payload["path"] == "x.txt"


def test_relay_helper_ignores_empty_tool():
    """tool_name 없으면(라이프사이클 이벤트 등) relay 안 함."""
    import subagent_coder as sc
    from subagent_coder import coder_event_bus
    captured = []
    with patch.object(coder_event_bus, "dispatch",
                      lambda rid, p: captured.append(1)):
        sc._relay_child_tool_call("rid", None, None, None)
    assert captured == []
