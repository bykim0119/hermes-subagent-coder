"""역할 fleet — 역할표 + 분기 + Planner 스폰 설정 검증."""
from unittest.mock import MagicMock, patch

from subagent_coder import coder_roles as cr
from subagent_coder import delegate_background as db


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


def test_register_stores_role():
    db._CODER_RUN_REGISTRY.clear()
    db._register_coder_run("r1", "parent", "g", role="planner")
    assert db._CODER_RUN_REGISTRY["r1"]["role"] == "planner"
    db._CODER_RUN_REGISTRY.clear()


def test_spawn_coder_uses_codex_path():
    """coder 역할: _coder_child_ctx에 codex provider 세팅 + terminal/file 툴셋."""
    db._CODER_RUN_REGISTRY.clear()
    db._register_coder_run("rc", "parent", "g", role="coder")
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        captured["ctx"] = db._coder_child_ctx.get()
        return "coder-done"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task), \
         patch("subagent_coder.codex_exec_client.register_coder_sink"), \
         patch("subagent_coder.codex_exec_client.unregister_coder_sink"), \
         patch("subagent_coder.coder_orchestration.notify_main_on_completion"):
        from subagent_coder.coder_roles import get_role
        db._spawn_detached_coder(MagicMock(), "g", "", "rc", get_role("coder"))
        import time; time.sleep(0.2)

    assert captured["toolsets"] == ["terminal", "file"]
    assert captured["ctx"]["provider"] == "codex-exec"   # codex override 적용
    assert captured["goal"] == "g"                        # 안내문 prefix 없음
    db._CODER_RUN_REGISTRY.clear()


def test_spawn_planner_uses_reasoning_path():
    """planner 역할: codex ctx 없음, file 툴셋, goal 앞에 설계자 안내문."""
    db._CODER_RUN_REGISTRY.clear()
    db._register_coder_run("rp", "parent", "그 기능 설계해", role="planner")
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        captured["ctx"] = db._coder_child_ctx.get()
        return "plan at docs/.../x.md"

    with patch("tools.delegate_tool.delegate_task", fake_delegate_task), \
         patch("subagent_coder.codex_exec_client.register_coder_sink"), \
         patch("subagent_coder.codex_exec_client.unregister_coder_sink"), \
         patch("subagent_coder.coder_orchestration.notify_main_on_completion"):
        from subagent_coder.coder_roles import get_role
        db._spawn_detached_coder(MagicMock(), "그 기능 설계해", "", "rp", get_role("planner"))
        import time; time.sleep(0.2)

    assert captured["toolsets"] == ["file"]
    assert captured["ctx"]["provider"] is None            # codex override 없음 → 메인 모델
    assert "설계자" in captured["goal"]                    # 안내문 prefix 주입
    assert "그 기능 설계해" in captured["goal"]            # 원 목표 포함
    db._CODER_RUN_REGISTRY.clear()


def test_delegate_background_routes_role():
    db._CODER_RUN_REGISTRY.clear()
    seen = {}

    def fake_spawn(parent_agent, goal, context, coder_run_id, role_config):
        seen["role"] = role_config.name
        return coder_run_id

    agent = MagicMock(); agent.task_id = "t"
    with patch.object(db, "_spawn_detached_coder", fake_spawn), \
         patch("subagent_coder.coder_config.check_codex_auth", return_value=None):
        out = db.delegate_task_background(parent_agent=agent, goal="g", role="planner")
    assert out["role"] == "planner"
    assert seen["role"] == "planner"
    db._CODER_RUN_REGISTRY.clear()


def test_delegate_background_defaults_to_coder():
    db._CODER_RUN_REGISTRY.clear()
    seen = {}

    def fake_spawn(parent_agent, goal, context, coder_run_id, role_config):
        seen["role"] = role_config.name
        return coder_run_id

    agent = MagicMock(); agent.task_id = "t"
    with patch.object(db, "_spawn_detached_coder", fake_spawn), \
         patch("subagent_coder.coder_config.check_codex_auth", return_value=None):
        out = db.delegate_task_background(parent_agent=agent, goal="g")   # role 미지정
    assert out["role"] == "coder"
    assert seen["role"] == "coder"
    db._CODER_RUN_REGISTRY.clear()


def test_schema_has_role_enum():
    enum = db.DELEGATE_TASK_BACKGROUND_SCHEMA["properties"]["role"]["enum"]
    assert "coder" in enum and "planner" in enum
