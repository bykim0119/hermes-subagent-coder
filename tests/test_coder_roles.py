"""역할 fleet — 역할표 + 분기 + Planner 스폰 설정 검증."""
from subagent_coder import coder_roles as cr


def test_registry_has_coder_and_planner():
    assert "coder" in cr.ROLE_REGISTRY
    assert "planner" in cr.ROLE_REGISTRY


def test_coder_role_uses_codex_and_exec_tools():
    coder = cr.get_role("coder")
    assert coder.provider == "codex-exec"
    assert "terminal" in coder.toolsets and "file" in coder.toolsets
    assert coder.instructions == ""


def test_planner_role_is_reasoning_model_no_terminal():
    p = cr.get_role("planner")
    assert p.provider is None                  # 메인 모델 상속
    assert "terminal" not in p.toolsets        # 코드 실행 불가
    assert "file" in p.toolsets                # 계획서 파일 쓰기/조사
    assert "계획" in p.instructions             # 설계자 안내문
    assert p.result_label == "플래너"


def test_get_role_defaults_and_fallback():
    assert cr.get_role(None).name == "coder"          # 미지정 → coder
    assert cr.get_role("nonsense").name == "coder"    # 미지의 → coder 폴백
