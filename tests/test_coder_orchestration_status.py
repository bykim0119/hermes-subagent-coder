"""코더 오케스트레이션 — 라우팅 캡처 + 조회 헬퍼 + 도구 단위 테스트."""
from unittest.mock import MagicMock

import pytest

from subagent_coder import delegate_background as db


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
