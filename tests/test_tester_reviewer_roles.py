"""역할 fleet 2차 — tester·reviewer 등록·필드·스폰·안내문·enum 검증."""
from subagent_coder import coder_roles as cr


def test_tester_role_codex_with_test_instructions():
    t = cr.get_role("tester")
    assert t.provider == "codex-exec"                 # 코더와 동일 실행력
    assert "terminal" in t.toolsets and "file" in t.toolsets
    assert "테스트" in t.instructions                  # 테스트 특화 안내문
    assert t.result_label == "테스터"


def test_reviewer_role_reasoning_with_pii_toolset():
    r = cr.get_role("reviewer")
    assert r.provider is None                          # 메인 추론모델 상속
    assert "terminal" not in r.toolsets                # 실행 불가(읽기/비평)
    assert "file" in r.toolsets and "pii" in r.toolsets  # 읽기 + 개인정보 스캔
    assert "개인정보" in r.instructions                 # 개인정보 점검 안내
    assert "scan_pii" in r.instructions                 # 도구 사용 절차
    assert r.result_label == "리뷰어"


def test_reviewer_completion_suffix_holds_for_boss():
    r = cr.get_role("reviewer")
    assert "공개" in r.completion_suffix and "보스" in r.completion_suffix
