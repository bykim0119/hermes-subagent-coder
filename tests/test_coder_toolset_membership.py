"""S4(toolsets) — _install_coder_toolset_membership 검증.

stock toolsets.py를 안 건드리고, register(ctx)가 delegate_task_background를
delegate_task가 있는 모든 toolset(by-reference core + concatenation 복사본 +
delegation)에 런타임 추가하는지 확인.
"""
import toolsets
from toolsets import resolve_toolset

from subagent_coder import _install_coder_toolset_membership


def test_added_to_core_and_delegation():
    _install_coder_toolset_membership()
    assert "delegate_task_background" in toolsets._HERMES_CORE_TOOLS
    # delegation toolset (자체 리스트)
    assert "delegate_task_background" in resolve_toolset("delegation")


def test_by_reference_toolset_gets_it():
    """_HERMES_CORE_TOOLS를 참조로 쓰는 toolset (in-place mutation)."""
    _install_coder_toolset_membership()
    assert "delegate_task_background" in resolve_toolset("hermes-cli")


def test_concatenation_toolsets_get_it():
    """import 시 _HERMES_CORE_TOOLS + [...]로 별도 리스트가 된 toolset들 —
    in-place mutation을 못 보므로 제네릭 스캔이 잡아야 함. hermes-discord가 핵심."""
    _install_coder_toolset_membership()
    for ts_name in ("hermes-discord", "hermes-feishu", "hermes-yuanbao"):
        resolved = resolve_toolset(ts_name)
        assert "delegate_task_background" in resolved, f"{ts_name} 누락"
        # delegate_task도 여전히 있어야(회귀 아님)
        assert "delegate_task" in resolved


def test_idempotent():
    _install_coder_toolset_membership()
    _install_coder_toolset_membership()
    # 중복 추가 안 됨 — underlying 리스트 직접 확인(resolve_toolset은 set이라 dedupe됨)
    assert toolsets._HERMES_CORE_TOOLS.count("delegate_task_background") == 1
    assert toolsets.TOOLSETS["delegation"]["tools"].count("delegate_task_background") == 1
    assert toolsets.TOOLSETS["hermes-discord"]["tools"].count("delegate_task_background") == 1
