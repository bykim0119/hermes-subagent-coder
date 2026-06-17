"""코더 오케스트레이션 — 라우팅 캡처 + 조회 헬퍼 + 도구 단위 테스트."""
from unittest.mock import MagicMock, patch

import pytest

from agent_company import orchestration as orch
from agent_company import delegate_background as db


@pytest.fixture(autouse=True)
def _clean_registry():
    db._CODER_RUN_REGISTRY.clear()
    yield
    db._CODER_RUN_REGISTRY.clear()


def _src(platform="discord", chat_id="C1", thread_id="T1"):
    s = MagicMock()
    s.platform = platform
    s.chat_id = chat_id
    s.thread_id = thread_id
    return s


def test_record_main_routing_marks_orchestration():
    db._register_coder_run("coder-r1", "parent", "goal")
    src = _src()
    db.record_main_routing("coder-r1", src, loop="LOOP")
    rec = db._CODER_RUN_REGISTRY["coder-r1"]
    assert rec["main_source"] is src
    assert rec["main_loop"] == "LOOP"


def test_list_orchestration_runs_filters_out_code_runs():
    # 오케스트레이션 런 (라우팅 기록됨)
    db._register_coder_run("coder-orch", "parent", "orch goal")
    db.record_main_routing("coder-orch", _src(), loop="LOOP")
    # /code 런 (라우팅 없음)
    db._register_coder_run("coder-slash", "slash:/code:42", "slash goal")

    runs = db.list_orchestration_runs()
    ids = {r["coder_run_id"] for r in runs}
    assert ids == {"coder-orch"}
    assert runs[0]["goal"] == "orch goal"
    assert runs[0]["status"] == "running"


def test_get_orchestration_run_excludes_code_runs():
    db._register_coder_run("coder-slash2", "slash:/code:42", "slash goal")
    assert db.get_orchestration_run("coder-slash2") is None


def test_get_orchestration_run_include_result_and_log():
    db._register_coder_run("coder-d", "parent", "goal")
    db.record_main_routing("coder-d", _src(), loop="LOOP")
    rec = db._CODER_RUN_REGISTRY["coder-d"]
    rec["status"] = "completed"
    rec["result"] = "all done"
    rec["log"].append({"event": "agent.message", "data": {"text": "x"}})

    detail = db.get_orchestration_run("coder-d", include=["result", "log"])
    assert detail["status"] == "completed"
    assert detail["result"] == "all done"
    assert detail["log"] == [{"event": "agent.message", "data": {"text": "x"}}]

    # include 없으면 result/log 미포함
    bare = db.get_orchestration_run("coder-d")
    assert "result" not in bare
    assert "log" not in bare


def test_coder_status_summary_lists_capacity(monkeypatch):
    monkeypatch.setattr(orch, "_max_concurrent", lambda: 3)
    db._register_coder_run("coder-a", "parent", "ga")
    db.record_main_routing("coder-a", _src(), loop="LOOP")
    db._register_coder_run("coder-b", "parent", "gb")
    db.record_main_routing("coder-b", _src(), loop="LOOP")
    db._CODER_RUN_REGISTRY["coder-b"]["status"] = "completed"
    # /code 런은 카운트/목록 제외
    db._register_coder_run("coder-slash", "slash:/code:1", "gs")

    out = orch.coder_status()
    assert out["active"] == 1          # running 인 오케스트레이션 런만
    assert out["max"] == 3
    assert out["available"] == 2
    assert {r["coder_run_id"] for r in out["runs"]} == {"coder-a", "coder-b"}


def test_coder_status_detail_with_include():
    db._register_coder_run("coder-d", "parent", "gd")
    db.record_main_routing("coder-d", _src(), loop="LOOP")
    rec = db._CODER_RUN_REGISTRY["coder-d"]
    rec["status"] = "completed"
    rec["result"] = "done!"

    out = orch.coder_status("coder-d", include=["result"])
    assert out["coder_run_id"] == "coder-d"
    assert out["result"] == "done!"


def test_coder_status_detail_log_tail_capped():
    db._register_coder_run("coder-d", "parent", "gd")
    db.record_main_routing("coder-d", _src(), loop="LOOP")
    rec = db._CODER_RUN_REGISTRY["coder-d"]
    for i in range(orch._LOG_TAIL + 5):
        rec["log"].append({"event": "e", "data": {"i": i}})

    out = orch.coder_status("coder-d", include=["log"])
    assert len(out["log"]) == orch._LOG_TAIL           # tail로 잘림
    assert out["log"][-1]["data"]["i"] == orch._LOG_TAIL + 4


def test_coder_status_unknown_or_code_run_errors():
    db._register_coder_run("coder-slash", "slash:/code:1", "gs")
    assert "error" in orch.coder_status("coder-slash")   # /code 제외
    assert "error" in orch.coder_status("nope")          # 미존재


def test_cancel_coder_wraps_cancel_run(monkeypatch):
    db._register_coder_run("coder-c", "parent", "gc")
    db.record_main_routing("coder-c", _src(), loop="LOOP")

    called = {}

    def fake_cancel(cid):
        called["cid"] = cid
        return True

    monkeypatch.setattr(orch, "cancel_coder_run", fake_cancel)
    out = orch.cancel_coder("coder-c")
    assert out == {"cancelled": True}
    assert called["cid"] == "coder-c"


def test_cancel_coder_rejects_code_run(monkeypatch):
    db._register_coder_run("coder-slash", "slash:/code:1", "gs")
    fired = {"n": 0}
    monkeypatch.setattr(
        orch, "cancel_coder_run", lambda cid: fired.__setitem__("n", fired["n"] + 1)
    )
    out = orch.cancel_coder("coder-slash")
    assert "error" in out
    assert fired["n"] == 0   # /code 런엔 cancel_coder_run 호출 안 함


def test_cancel_coder_missing_id():
    assert "error" in orch.cancel_coder("")


def test_register_and_membership():
    import toolsets
    from toolsets import resolve_toolset

    orch.register_orchestration_tools()
    orch.install_orchestration_toolset_membership()

    from tools.delegate_tool import registry
    assert registry.get_entry("coder_status") is not None
    assert registry.get_entry("cancel_coder") is not None

    # delegate_task가 있는 toolset에 두 도구가 들어감
    for ts_name in ("delegation", "hermes-discord"):
        resolved = resolve_toolset(ts_name)
        assert "coder_status" in resolved, f"{ts_name} coder_status 누락"
        assert "cancel_coder" in resolved, f"{ts_name} cancel_coder 누락"

    # 멱등
    orch.install_orchestration_toolset_membership()
    assert toolsets._HERMES_CORE_TOOLS.count("coder_status") == 1


# --- 폴링 억제 안내(yield guidance) -----------------------------------------

def test_delegate_background_return_has_yield_note():
    agent = MagicMock()
    agent.task_id = "t1"
    with patch.object(db, "_spawn_detached_coder"), \
         patch("agent_company.config.check_codex_auth", return_value=None):
        result = db.delegate_task_background(parent_agent=agent, goal="g")
    assert result["status"] == "spawned"
    assert result["note"] == db.YIELD_NOTE


def test_coder_status_summary_has_yield_note():
    out = orch.coder_status()
    assert out["note"] == db.YIELD_NOTE


def test_coder_status_detail_has_no_note():
    db._register_coder_run("coder-n", "parent", "g")
    db.record_main_routing("coder-n", _src(), loop="LOOP")
    out = orch.coder_status("coder-n")
    assert "note" not in out


def test_status_summary_includes_role():
    db._register_coder_run("rrole", "parent", "g", role="planner")
    db.record_main_routing("rrole", _src(), loop="LOOP")
    runs = db.list_orchestration_runs()
    assert any(r["coder_run_id"] == "rrole" and r["role"] == "planner" for r in runs)


def test_status_detail_includes_role():
    db._register_coder_run("rrole2", "parent", "g", role="planner")
    db.record_main_routing("rrole2", _src(), loop="LOOP")
    detail = db.get_orchestration_run("rrole2")
    assert detail["role"] == "planner"
