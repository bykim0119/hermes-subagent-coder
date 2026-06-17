"""역할 fleet 2차 — tester·reviewer 등록·필드·스폰·안내문·enum 검증."""
from unittest.mock import MagicMock, patch

from subagent_coder import coder_roles as cr
from subagent_coder import delegate_background as db


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


def test_spawn_tester_codex_path_injects_instructions():
    """tester: codex override(provider=codex-exec) + 안내문이 goal 앞에 주입."""
    db._CODER_RUN_REGISTRY.clear()
    db._register_coder_run("rt", "parent", "그 모듈 테스트해", role="tester")
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        captured["ctx"] = db._coder_child_ctx.get()
        return "tested"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task), \
         patch("subagent_coder.codex_exec_client.register_coder_sink"), \
         patch("subagent_coder.codex_exec_client.unregister_coder_sink"), \
         patch("subagent_coder.coder_orchestration.notify_main_on_completion"):
        from subagent_coder.coder_roles import get_role
        db._spawn_detached_coder(MagicMock(), "그 모듈 테스트해", "", "rt", get_role("tester"))
        import time; time.sleep(0.2)

    assert captured["ctx"]["provider"] == "codex-exec"   # 코더 경로(codex override)
    assert captured["toolsets"] == ["terminal", "file"]
    assert "테스터" in captured["goal"]                   # 안내문 prepend 됨
    assert "그 모듈 테스트해" in captured["goal"]          # 원 목표 포함
    db._CODER_RUN_REGISTRY.clear()


def test_spawn_reviewer_reasoning_path_with_pii_toolset():
    """reviewer: codex override 없음 + file,pii 툴셋 + 안내문 prepend."""
    db._CODER_RUN_REGISTRY.clear()
    db._register_coder_run("rr", "parent", "이 결과물 리뷰해", role="reviewer")
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        captured["ctx"] = db._coder_child_ctx.get()
        return "reviewed"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task), \
         patch("subagent_coder.codex_exec_client.register_coder_sink"), \
         patch("subagent_coder.codex_exec_client.unregister_coder_sink"), \
         patch("subagent_coder.coder_orchestration.notify_main_on_completion"):
        from subagent_coder.coder_roles import get_role
        db._spawn_detached_coder(MagicMock(), "이 결과물 리뷰해", "", "rr", get_role("reviewer"))
        import time; time.sleep(0.2)

    assert captured["ctx"]["provider"] is None            # 메인 모델(코덱스 override 없음)
    assert captured["ctx"]["use_codex"] is False
    assert captured["toolsets"] == ["file", "pii"]
    assert "리뷰어" in captured["goal"]                    # 안내문 prepend
    db._CODER_RUN_REGISTRY.clear()


def test_schema_role_enum_has_four_roles():
    enum = db.DELEGATE_TASK_BACKGROUND_SCHEMA["properties"]["role"]["enum"]
    assert set(enum) == {"coder", "planner", "tester", "reviewer"}
