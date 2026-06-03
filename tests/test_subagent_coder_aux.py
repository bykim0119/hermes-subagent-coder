"""subagent_coder.register(ctx)가 codex-exec를 hermes_cli.auth에 등록해야 한다.

stock auth.py는 codex-exec를 모르고 resolve_external_process_provider_credentials는
copilot-acp 전용 하드코드다. register(ctx)는 (1) PROVIDER_REGISTRY에 codex-exec
ProviderConfig를 추가하고 (2) resolver를 wrap해 codex-exec를 plugin이 해석하게
한다 — 코더 wiring을 한 plugin으로 모으는 P1 원칙과 일관 (auth.py 흔적 0).
"""
from unittest.mock import MagicMock


def test_codex_exec_registered_after_register():
    import hermes_cli.auth as auth

    # 격리: 이전 테스트가 이미 등록했을 수 있으니 register만으로 복원되는지
    auth.PROVIDER_REGISTRY.pop("codex-exec", None)

    from subagent_coder import register

    register(MagicMock())

    assert "codex-exec" in auth.PROVIDER_REGISTRY, \
        "register(ctx)가 codex-exec를 PROVIDER_REGISTRY에 추가하지 않음"
    pconfig = auth.PROVIDER_REGISTRY["codex-exec"]
    assert pconfig.auth_type == "external_process"
    assert pconfig.inference_base_url == "codex-exec://local"
    assert getattr(auth, "_subagent_coder_resolver_wrapped", False), \
        "resolve_external_process_provider_credentials가 wrap되지 않음"


def test_resolve_codex_exec_via_wrap(monkeypatch):
    """wrap된 resolver가 codex-exec creds를 올바르게 반환."""
    import hermes_cli.auth as auth
    from subagent_coder import _install_codex_exec_auth

    monkeypatch.delenv("HERMES_CODER_COMMAND", raising=False)
    monkeypatch.delenv("HERMES_CODER_ARGS", raising=False)
    monkeypatch.setattr(
        "hermes_cli.auth.shutil.which", lambda command: f"/usr/local/bin/{command}"
    )
    _install_codex_exec_auth()

    creds = auth.resolve_external_process_provider_credentials("codex-exec")
    assert creds["provider"] == "codex-exec"
    assert creds["api_key"] == "codex-exec"
    assert creds["base_url"] == "codex-exec://local"
    assert creds["command"] == "/usr/local/bin/codex"
    assert "--skip-git-repo-check" in creds["args"]
    assert creds["source"] == "process"


def test_copilot_acp_still_falls_through_to_stock(monkeypatch):
    """비-codex 외부프로세스 provider는 stock resolver로 위임."""
    import hermes_cli.auth as auth
    from subagent_coder import _install_codex_exec_auth

    monkeypatch.setenv("HERMES_COPILOT_ACP_ARGS", "--acp --stdio")
    monkeypatch.setattr(
        "hermes_cli.auth.shutil.which", lambda command: f"/usr/local/bin/{command}"
    )
    _install_codex_exec_auth()

    creds = auth.resolve_external_process_provider_credentials("copilot-acp")
    assert creds["provider"] == "copilot-acp"
    assert creds["api_key"] == "copilot-acp"
    assert creds["command"] == "/usr/local/bin/copilot"
