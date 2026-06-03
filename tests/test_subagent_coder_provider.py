"""subagent_coder가 register(ctx) 시 codex-exec model provider를 등록해야 한다.

provider 등록은 ctx가 아니라 providers 전역 레지스트리(register_provider)를 통한다
(hermes 실제 메커니즘). 따라서 register(ctx) 후 get_provider_profile로 확인한다.
"""
from unittest.mock import MagicMock


def test_register_adds_codex_exec_provider():
    import providers

    # 격리: 다른 import로 이미 등록됐을 수 있으니 제거 후 register만으로 복원되는지 검증
    providers._REGISTRY.pop("codex-exec", None)

    from subagent_coder import register

    register(MagicMock())

    prof = providers.get_provider_profile("codex-exec")
    assert prof is not None, "register(ctx)가 codex-exec provider를 등록하지 않음"
    assert prof.name == "codex-exec"
    assert prof.auth_type == "external_process"
