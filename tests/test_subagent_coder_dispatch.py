"""register(ctx)가 AIAgent._invoke_tool을 wrap해 delegate_task_background에
parent_agent를 주입하는지 검증.

이게 독립 plugin의 핵심: stock run_agent.py를 안 건드리고, registry dispatch가
주지 않는 parent_agent를 런타임 wrap으로 주입한다. (concurrent 경로 = _invoke_tool)
"""
import json
from unittest.mock import MagicMock, patch


def test_register_wraps_invoke_tool_and_injects_parent_agent():
    from subagent_coder import register
    from run_agent import AIAgent

    register(MagicMock())  # plugin enable 시뮬

    assert getattr(AIAgent, "_subagent_coder_dispatch_wrapped", False), \
        "register가 _invoke_tool을 wrap하지 않음"

    agent = MagicMock()  # 현재 실행 중인 AIAgent 역할
    with patch("subagent_coder.delegate_background._spawn_detached_coder"), \
         patch("subagent_coder.coder_config.check_codex_auth", return_value=None):
        result = json.loads(
            AIAgent._invoke_tool(agent, "delegate_task_background", {"goal": "g"})
        )

    # parent_agent=self(agent)가 주입돼 정상 spawn + callback 발화
    assert result["status"] == "spawned"
    assert result["coder_run_id"].startswith("agent-")
    agent.coder_spawn_callback.assert_called_once()


def test_wrap_is_idempotent():
    """register가 여러 번 불려도 wrap은 한 번만 (이중 wrap 방지)."""
    from subagent_coder import register
    from run_agent import AIAgent

    register(MagicMock())
    first = AIAgent._invoke_tool
    register(MagicMock())
    assert AIAgent._invoke_tool is first, "register 재호출 시 이중 wrap 발생"
