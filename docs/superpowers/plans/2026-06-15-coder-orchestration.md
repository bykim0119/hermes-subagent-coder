# 코더 오케스트레이션 확장 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 메인 에이전트(gpt-5.5)가 agent-spawn된 코더를 관찰(`coder_status`)·취소(`cancel_coder`)하고, 코더 완료 시 자동으로 새 턴으로 깨어나(completion wake) 다음 단계를 스케줄할 수 있게 한다.

**Architecture:** stock hermes는 손대지 않는다(diff 0). 신규 도구·완료알림 로직은 새 모듈 `coder_orchestration.py`로 외부화하고, registry 레코드 조작(라우팅 메타데이터·로그 deque·완료알림 claim)만 기존 `delegate_background.py`에 추가한다. "오케스트레이션 대상"은 레코드의 `main_source`(메인 세션 라우팅 메타데이터) 존재 여부 단일 게이트로 판별한다 — `delegate_task_background` 경로에서만 기록되고, `/code` 슬래시 경로는 자동 제외된다. 완료알림은 stock의 백그라운드-프로세스 완료 패턴(`MessageEvent(internal=True)` + `adapter.handle_message`, `gateway/run.py:16040`)을 `gateway.run._gateway_runner_ref` 브리지로 그대로 미러링한다.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, hermes-agent 0.16 (`~/.venvs/hermes-stock-016`), monkey-patch 기반 plugin wiring, `unittest.mock` fake 어댑터.

**테스트 명령(모든 Task 공통):**
```bash
cd /mnt/e/ipynbs_port/legacy/nous_hermes/hermes-subagent-coder
~/.venvs/hermes-stock-016/bin/python -m pytest tests/ -q
```
개별 테스트: 위 명령의 `tests/` 자리에 `tests/<file>::<test>` 를 넣는다.

**현재 베이스라인:** 브랜치 `feature/coder-orchestration` (0.16 포트 위), 기존 48 테스트 통과.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `delegate_background.py` | registry 레코드 조작: 라우팅 메타데이터 저장, 로그 deque, 오케스트레이션 런 조회, 완료알림 claim, cancelled-status 보호 | Modify |
| `coder_orchestration.py` | 신규 도구(`coder_status`/`cancel_coder`) + 완료 웨이크 + 도구 등록 + toolset membership | **Create** |
| `__init__.py` | `register(ctx)`에서 신규 도구 등록 + membership 호출, 스폰 콜백에서 라우팅 기록 | Modify |
| `tests/test_coder_orchestration_status.py` | `coder_status`/`cancel_coder`/membership/`/code` 제외 단위 테스트 | **Create** |
| `tests/test_coder_orchestration_wake.py` | 완료 웨이크(성공/실패/취소)·중복방지·라우팅/로그 캡처 | **Create** |
| `tests/test_coder_toolset_membership.py` | 신규 두 도구의 membership 회귀 추가 | Modify |

---

## Task 1: 로그 캡처 탭 + 레코드 deque

**Files:**
- Modify: `delegate_background.py` (imports, `_register_coder_run`, `_build_coder_progress_sink`)
- Test: `tests/test_coder_orchestration_wake.py`

진단·실패알림용으로 각 코더 런의 NDJSON 이벤트를 bounded deque에 보관한다. 현재 sink는 이벤트버스로 흘려보내기만 하고 보관하지 않는다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_coder_orchestration_wake.py` 생성:

```python
"""코더 오케스트레이션 — 로그 캡처 + 완료 웨이크 검증."""
import sys
import types
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from subagent_coder import delegate_background as db


@pytest.fixture(autouse=True)
def _clean_registry():
    db._CODER_RUN_REGISTRY.clear()
    yield
    db._CODER_RUN_REGISTRY.clear()


def _evt(name, data=None):
    e = MagicMock()
    e.event = name
    e.data = data or {}
    return e


def test_register_seeds_log_deque():
    db._register_coder_run("coder-log1", "parent", "goal")
    rec = db._CODER_RUN_REGISTRY["coder-log1"]
    assert isinstance(rec["log"], deque)
    assert rec["log"].maxlen == db._LOG_MAXLEN


def test_sink_captures_events_into_log():
    db._register_coder_run("coder-log2", "parent", "goal")
    sink = db._build_coder_progress_sink("coder-log2")
    sink(_evt("agent.thinking", {"text": "hi"}))
    sink(_evt("agent.message", {"text": "done"}))
    rec = db._CODER_RUN_REGISTRY["coder-log2"]
    captured = list(rec["log"])
    assert captured == [
        {"event": "agent.thinking", "data": {"text": "hi"}},
        {"event": "agent.message", "data": {"text": "done"}},
    ]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_wake.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_LOG_MAXLEN'` / `KeyError: 'log'`

- [ ] **Step 3: 최소 구현**

`delegate_background.py` 상단 import에 deque 추가 (기존 `import threading` 줄 아래):

```python
from collections import deque
```

`_CODER_RUN_LOCK = threading.Lock()` 아래에 상수 추가:

```python
_LOG_MAXLEN = 200  # bounded coder NDJSON event tail per run
```

`_register_coder_run` 의 레코드 dict에 `"log"` 추가:

```python
def _register_coder_run(coder_run_id: str, parent_task_id: str, goal: str) -> None:
    with _CODER_RUN_LOCK:
        _CODER_RUN_REGISTRY[coder_run_id] = {
            "parent_task_id": parent_task_id,
            "goal": goal,
            "started_at": time.time(),
            "status": "running",
            "log": deque(maxlen=_LOG_MAXLEN),
        }
```

`_build_coder_progress_sink._sink` 의 `from . import coder_event_bus` 줄 **직전**에 로그 캡처 블록 삽입:

```python
            try:
                with _CODER_RUN_LOCK:
                    rec = _CODER_RUN_REGISTRY.get(coder_run_id)
                    if rec is not None and rec.get("log") is not None:
                        rec["log"].append({"event": event.event, "data": event.data})
            except Exception:
                logger.debug("coder log capture failed", exc_info=True)
            from . import coder_event_bus
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_wake.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: 커밋**

```bash
git add delegate_background.py tests/test_coder_orchestration_wake.py
git commit -m "feat(orchestration): capture coder NDJSON event log tail per run"
```

---

## Task 2: 메인 세션 라우팅 메타데이터 캡처 + 오케스트레이션 런 조회

**Files:**
- Modify: `delegate_background.py` (신규 함수 4개)
- Modify: `__init__.py` (`_build_coder_spawn_callback._coder_spawn`)
- Test: `tests/test_coder_orchestration_status.py`, `tests/test_coder_orchestration_wake.py`

레코드에 `main_source`(메인 세션 `SessionSource`)와 `main_loop`(게이트웨이 이벤트 루프)를 저장한다. 이 `main_source` 존재가 "오케스트레이션 대상" 단일 게이트다. 조회용 헬퍼(`list_orchestration_runs`/`get_orchestration_run`)도 추가한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_coder_orchestration_status.py` 생성:

```python
"""코더 오케스트레이션 — 라우팅 캡처 + 조회 헬퍼 단위 테스트."""
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'record_main_routing'`

- [ ] **Step 3: 최소 구현**

`delegate_background.py` 의 `get_coder_run` 함수 **아래**에 4개 함수 추가:

```python
def record_main_routing(coder_run_id: str, source: Any, loop: Any) -> None:
    """메인 세션 라우팅 메타데이터 + 이벤트 루프를 런 레코드에 저장.

    ``main_source`` 존재가 런을 *오케스트레이션 대상*으로 표시하는 단일 게이트다
    (agent의 delegate_task_background 경로에서만 기록; /code 슬래시는 기록 안 함).
    오케스트레이션 런만 coder_status에 보이고, cancel_coder로 취소되며, 완료 시
    메인을 깨운다.
    """
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is not None:
            rec["main_source"] = source
            rec["main_loop"] = loop


def list_orchestration_runs() -> List[Dict[str, Any]]:
    """오케스트레이션 런 전체의 요약 리스트(라우팅 없는 /code 런은 제외)."""
    with _CODER_RUN_LOCK:
        return [
            {
                "coder_run_id": cid,
                "goal": rec.get("goal"),
                "status": rec.get("status"),
                "started_at": rec.get("started_at"),
            }
            for cid, rec in _CODER_RUN_REGISTRY.items()
            if rec.get("main_source") is not None
        ]


def get_orchestration_run(
    coder_run_id: str, include: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """단일 오케스트레이션 런 상세. 라우팅 없는 런이면 None.

    ``include``에 "result"가 있으면 result/error, "log"가 있으면 log 전체를 포함.
    """
    wanted = set(include or [])
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is None or rec.get("main_source") is None:
            return None
        out: Dict[str, Any] = {
            "coder_run_id": coder_run_id,
            "goal": rec.get("goal"),
            "status": rec.get("status"),
            "started_at": rec.get("started_at"),
            "parent_task_id": rec.get("parent_task_id"),
        }
        if "result" in wanted:
            if rec.get("result") is not None:
                out["result"] = rec.get("result")
            if rec.get("error") is not None:
                out["error"] = rec.get("error")
        if "log" in wanted:
            out["log"] = list(rec.get("log") or [])
        return out


def claim_completion_notify(coder_run_id: str) -> Optional[Dict[str, Any]]:
    """완료 알림 1회 권한을 원자적으로 claim.

    이 호출이 claim에 성공하면(오케스트레이션 런 + 미알림) ``notified`` 플래그를
    세팅하고 스냅샷 dict를 반환한다 → 동시/중복 완료가 정확히 1회만 주입되도록 보장.
    그 외(라우팅 없음 / 이미 알림)는 None.
    """
    with _CODER_RUN_LOCK:
        rec = _CODER_RUN_REGISTRY.get(coder_run_id)
        if rec is None or rec.get("main_source") is None:
            return None
        if rec.get("notified"):
            return None
        rec["notified"] = True
        return {
            "goal": rec.get("goal"),
            "status": rec.get("status"),
            "result": rec.get("result"),
            "error": rec.get("error"),
            "source": rec.get("main_source"),
            "loop": rec.get("main_loop"),
            "log": list(rec.get("log") or []),
        }
```

`__init__.py` 의 `_build_coder_spawn_callback` 안 `_coder_spawn` 클로저 **맨 위**(기존 `if not status_adapter ...` 가드 직전)에 라우팅 기록 추가:

```python
    def _coder_spawn(coder_run_id: str, goal: str) -> None:
        # 라우팅 메타데이터를 먼저 기록 — 스레드 생성 가드와 무관하게 완료 웨이크가
        # 동작하도록(메인 세션 source/loop는 이 클로저에 이미 캡처됨).
        try:
            from .delegate_background import record_main_routing
            record_main_routing(coder_run_id, source, loop)
        except Exception:
            logger.debug("record_main_routing failed", exc_info=True)
        if not status_adapter or not _run_still_current():
            return
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: 스폰 콜백 라우팅 기록 회귀 확인**

`tests/test_coder_orchestration_wake.py` 에 추가:

```python
def test_spawn_callback_records_routing():
    from subagent_coder import _build_coder_spawn_callback

    db._register_coder_run("coder-cb", "parent", "goal")
    adapter = MagicMock()
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    runner._is_session_run_current.return_value = True
    src = MagicMock()
    src.platform = "discord"
    src.chat_id = "C1"
    src.thread_id = "T1"

    cb = _build_coder_spawn_callback(runner, src, "skey", 1, loop="LOOP")
    with patch("asyncio.run_coroutine_threadsafe"):
        cb("coder-cb", "goal")

    rec = db._CODER_RUN_REGISTRY["coder-cb"]
    assert rec["main_source"] is src
    assert rec["main_loop"] == "LOOP"
```

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_wake.py -q`
Expected: PASS (3 passed)

- [ ] **Step 6: 커밋**

```bash
git add delegate_background.py __init__.py tests/test_coder_orchestration_status.py tests/test_coder_orchestration_wake.py
git commit -m "feat(orchestration): record main-session routing as orchestration gate + run queries"
```

---

## Task 3: `coder_status` 도구

**Files:**
- Create: `coder_orchestration.py`
- Test: `tests/test_coder_orchestration_status.py`

메인이 코더 용량·진행·결과를 조회하는 read 도구. id 생략 시 전체 요약 + 용량, id 지정 시 상세(`include`로 result/log).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_coder_orchestration_status.py` 에 추가 (상단 import에 `from subagent_coder import coder_orchestration as orch` 추가):

```python
from subagent_coder import coder_orchestration as orch


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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q -k coder_status`
Expected: FAIL — `ModuleNotFoundError: No module named 'subagent_coder.coder_orchestration'`

- [ ] **Step 3: 최소 구현**

`coder_orchestration.py` 생성 (이 Task는 status 부분만 — cancel/wake는 다음 Task에서 같은 파일에 추가):

```python
"""코더 오케스트레이션 도구 — 메인 에이전트의 코더 관찰·제어·완료알림.

agent-spawn된 코더("오케스트레이션 런")에 대한 메인 에이전트의 "시야"를 외부화한다:
  * ``coder_status`` (read)  — 오케스트레이션 코더 목록/상세 + 용량 조회.
  * ``cancel_coder`` (action) — 오케스트레이션 코더를 프로그래밍적으로 취소 (Task 4).
  * 완료 웨이크              — 코더 완료 시 메인 세션에 synthetic 내부 MessageEvent를
                               주입 (Task 5). stock 백그라운드-프로세스 알림 미러.

라우팅 메타데이터(``main_source``)를 가진 *오케스트레이션 런*만 대상이다. ``/code``
슬래시 코더는 라우팅이 없어 셋 다에서 제외된다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .delegate_background import (
    get_orchestration_run,
    list_orchestration_runs,
)

logger = logging.getLogger(__name__)

_LOG_TAIL = 20  # status/실패알림에 노출할 로그 tail 이벤트 수


def _max_concurrent() -> int:
    try:
        from .coder_config import coder_setting

        return coder_setting(
            "max_concurrent",
            env_var="HERMES_CODER_MAX_CONCURRENT",
            default=3,
            cast=int,
        )
    except Exception:
        return 3


def coder_status(
    coder_run_id: Optional[str] = None,
    include: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """오케스트레이션 코더 상태 조회.

    id 생략 → 전체 요약 + 용량(active/max/available + 각 런 요약).
    id 지정 → 해당 런 상세. ``include``에 "result"/"log"가 있으면 결과/로그 tail 포함.
    라우팅 없는(/code) 또는 미존재 id → {"error": ...}.
    """
    if coder_run_id:
        detail = get_orchestration_run(coder_run_id, include=include)
        if detail is None:
            return {
                "error": f"코더 '{coder_run_id}'는 오케스트레이션 대상이 아니거나 없음"
            }
        if "log" in detail:
            detail["log"] = detail["log"][-_LOG_TAIL:]
        return detail

    runs = list_orchestration_runs()
    active = sum(1 for r in runs if r.get("status") == "running")
    mx = _max_concurrent()
    return {
        "active": active,
        "max": mx,
        "available": max(mx - active, 0),
        "runs": runs,
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q -k coder_status`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
git add coder_orchestration.py tests/test_coder_orchestration_status.py
git commit -m "feat(orchestration): add coder_status read tool"
```

---

## Task 4: `cancel_coder` 도구

**Files:**
- Modify: `coder_orchestration.py`
- Test: `tests/test_coder_orchestration_status.py`

메인이 방향이 틀린 코더를 프로그래밍적으로 중단. 기존 `cancel_coder_run` 래핑, 오케스트레이션 대상에만 적용(/code 코더는 거부 — 사람의 Discord `!cancel` 경로 유지).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_coder_orchestration_status.py` 에 추가:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q -k cancel_coder`
Expected: FAIL — `AttributeError: module ... has no attribute 'cancel_coder'`

- [ ] **Step 3: 최소 구현**

`coder_orchestration.py` 의 import 블록에 `cancel_coder_run`, `get_orchestration_run`(이미 있음) 보강 — import 라인을 다음으로 교체:

```python
from .delegate_background import (
    cancel_coder_run,
    get_orchestration_run,
    list_orchestration_runs,
)
```

`coder_status` 함수 **아래**에 추가:

```python
def cancel_coder(coder_run_id: str) -> Dict[str, Any]:
    """오케스트레이션 코더를 프로그래밍적으로 취소(기존 cancel_coder_run 래핑).

    오케스트레이션 대상에만 적용한다. /code 슬래시 코더는 라우팅이 없어 거부되며
    기존 사람 경로(Discord ``!cancel``)로만 취소된다.
    """
    if not coder_run_id:
        return {"error": "coder_run_id required"}
    if get_orchestration_run(coder_run_id) is None:
        return {
            "error": f"코더 '{coder_run_id}'는 오케스트레이션 대상이 아니거나 없음"
        }
    return {"cancelled": bool(cancel_coder_run(coder_run_id))}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q -k cancel_coder`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add coder_orchestration.py tests/test_coder_orchestration_status.py
git commit -m "feat(orchestration): add cancel_coder action tool (orchestration runs only)"
```

---

## Task 5: 완료 웨이크 (synthetic 내부 MessageEvent 주입)

**Files:**
- Modify: `coder_orchestration.py` (`notify_main_on_completion` + 헬퍼)
- Modify: `delegate_background.py` (`_spawn_detached_coder._runner` finally + cancelled 보호)
- Test: `tests/test_coder_orchestration_wake.py`

코더 완료 시 stock의 백그라운드-프로세스 패턴을 미러해 메인 세션에 `MessageEvent(internal=True)`를 주입한다. 결과/에러/로그 tail을 텍스트에 담아 보낸다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_coder_orchestration_wake.py` 에 추가:

```python
from subagent_coder import coder_orchestration as orch


def _install_fake_gateway(monkeypatch, adapter):
    """gateway.run._gateway_runner_ref가 adapter를 가진 runner를 가리키게 한다."""
    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    fake_mod = types.ModuleType("gateway.run")
    fake_mod._gateway_runner_ref = lambda: runner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_mod)
    return runner


def _seed_orch(cid, status, **extra):
    db._register_coder_run(cid, "parent", extra.pop("goal", "goal"))
    src = MagicMock()
    src.platform = "discord"
    src.chat_id = "C1"
    src.thread_id = "T1"
    db.record_main_routing(cid, src, loop="LOOP")
    rec = db._CODER_RUN_REGISTRY[cid]
    rec["status"] = status
    rec.update(extra)
    return rec, src


def test_wake_success_injects_message(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    _seed_orch("coder-w1", "completed", result="built the thing")

    with patch("asyncio.run_coroutine_threadsafe") as sched:
        orch.notify_main_on_completion("coder-w1")

    assert sched.called
    # handle_message coroutine + 캡처된 loop
    assert sched.call_args.args[1] == "LOOP"
    synth = adapter.handle_message.call_args.args[0]
    assert synth.internal is True
    assert "완료" in synth.text and "built the thing" in synth.text


def test_wake_failure_includes_error_and_log(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    rec, _ = _seed_orch("coder-w2", "failed", error="boom")
    rec["log"].append({"event": "agent.message", "data": {"text": "last line"}})

    with patch("asyncio.run_coroutine_threadsafe"):
        orch.notify_main_on_completion("coder-w2")

    synth = adapter.handle_message.call_args.args[0]
    assert "실패" in synth.text and "boom" in synth.text and "last line" in synth.text


def test_wake_cancelled(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    _seed_orch("coder-w3", "cancelled")

    with patch("asyncio.run_coroutine_threadsafe"):
        orch.notify_main_on_completion("coder-w3")

    synth = adapter.handle_message.call_args.args[0]
    assert "취소" in synth.text


def test_wake_dedup_single_injection(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    _seed_orch("coder-w4", "completed", result="r")

    with patch("asyncio.run_coroutine_threadsafe") as sched:
        orch.notify_main_on_completion("coder-w4")
        orch.notify_main_on_completion("coder-w4")   # 두 번째는 claim 실패

    assert sched.call_count == 1


def test_wake_skips_code_run(monkeypatch):
    adapter = MagicMock()
    _install_fake_gateway(monkeypatch, adapter)
    db._register_coder_run("coder-slash", "slash:/code:1", "gs")
    db._CODER_RUN_REGISTRY["coder-slash"]["status"] = "completed"

    with patch("asyncio.run_coroutine_threadsafe") as sched:
        orch.notify_main_on_completion("coder-slash")

    assert not sched.called   # 라우팅 없음 → claim None → no-op
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_wake.py -q -k wake`
Expected: FAIL — `AttributeError: module ... has no attribute 'notify_main_on_completion'`

- [ ] **Step 3: 최소 구현**

`coder_orchestration.py` 상단 import에 `sys` 추가하고 `claim_completion_notify` 를 import에 보강 — 파일 상단을 다음으로 교체:

```python
from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from .delegate_background import (
    cancel_coder_run,
    claim_completion_notify,
    get_orchestration_run,
    list_orchestration_runs,
)
```

`cancel_coder` 함수 **아래**에 완료 웨이크 추가:

```python
def _format_log_line(evt: Dict[str, Any]) -> str:
    name = evt.get("event")
    data = evt.get("data")
    return f"{name}: {data}" if data else str(name)


def _build_completion_text(coder_run_id: str, snap: Dict[str, Any]) -> str:
    goal = snap.get("goal")
    status = snap.get("status")
    if status == "cancelled":
        return f"[코더 {coder_run_id} 취소됨] 작업:{goal}"
    if status == "failed":
        tail = "\n".join(_format_log_line(e) for e in (snap.get("log") or [])[-_LOG_TAIL:])
        return (
            f"[코더 {coder_run_id} 실패] 작업:{goal} 에러:{snap.get('error')}\n"
            f"최근 로그:\n{tail}"
        )
    return f"[코더 {coder_run_id} 완료] 작업:{goal}\n결과:{snap.get('result')}"


def _resolve_adapter(source):
    """gateway.run._gateway_runner_ref 브리지로 source.platform의 live 어댑터를 해석."""
    gw = sys.modules.get("gateway.run")
    ref = getattr(gw, "_gateway_runner_ref", None) if gw is not None else None
    runner = ref() if callable(ref) else None
    if runner is None:
        return None
    adapters = getattr(runner, "adapters", {})
    plat = getattr(source, "platform", None)
    adapter = adapters.get(plat)
    if adapter is None:
        pv = getattr(plat, "value", plat)
        for p, a in adapters.items():
            if getattr(p, "value", p) == pv:
                adapter = a
                break
    return adapter


def notify_main_on_completion(coder_run_id: str) -> None:
    """오케스트레이션 코더 완료 시 메인 세션에 synthetic 내부 MessageEvent 주입.

    stock 백그라운드-프로세스 완료 알림(gateway/run.py)의 정확한 미러:
    MessageEvent(internal=True) + adapter.handle_message. claim_completion_notify로
    완료당 1회만 주입한다. 코더 데몬 스레드에서 호출되므로 게이트웨이 루프에
    run_coroutine_threadsafe로 스케줄한다.
    """
    snap = claim_completion_notify(coder_run_id)
    if snap is None:
        return  # 오케스트레이션 대상 아님 또는 이미 알림
    source = snap.get("source")
    loop = snap.get("loop")
    if source is None or loop is None:
        logger.warning("코더 %s 완료 — 라우팅/루프 분실, 알림 drop", coder_run_id)
        return
    adapter = _resolve_adapter(source)
    if adapter is None:
        logger.warning(
            "코더 %s 완료 — adapter 분실, 알림 drop (결과는 coder_status로 조회 가능)",
            coder_run_id,
        )
        return
    text = _build_completion_text(coder_run_id, snap)
    try:
        import asyncio

        from gateway.platforms.base import MessageEvent, MessageType

        synth = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        asyncio.run_coroutine_threadsafe(adapter.handle_message(synth), loop)
        logger.info(
            "코더 %s 완료 — 메인 깨우기 주입 (status=%s)",
            coder_run_id,
            snap.get("status"),
        )
    except Exception:
        logger.exception("코더 %s 완료 알림 주입 실패", coder_run_id)
```

`delegate_background.py` 의 `_spawn_detached_coder._runner` 를 수정 — 성공/실패 양쪽에서 cancelled 상태를 덮어쓰지 않게 하고, finally에서 완료 웨이크 호출. 기존 try/except/finally를 다음으로 교체:

```python
        try:
            result = delegate_task(
                parent_agent=parent_agent,
                goal=goal,
                context=context,
                tasks=None,
                toolsets=["terminal", "file"],
                role="leaf",
            )
            with _CODER_RUN_LOCK:
                rec = _CODER_RUN_REGISTRY.get(coder_run_id)
                if rec is not None and rec.get("status") != "cancelled":
                    rec["status"] = "completed"
                    rec["result"] = result
        except Exception as exc:
            logger.exception("Coder run %s failed: %s", coder_run_id, exc)
            with _CODER_RUN_LOCK:
                rec = _CODER_RUN_REGISTRY.get(coder_run_id)
                if rec is not None and rec.get("status") != "cancelled":
                    rec["status"] = "failed"
                    rec["error"] = str(exc)
        finally:
            _coder_child_ctx.reset(token)
            unregister_coder_sink(coder_run_id)
            try:
                from . import coder_orchestration
                coder_orchestration.notify_main_on_completion(coder_run_id)
            except Exception:
                logger.debug("completion notify failed", exc_info=True)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_wake.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: 커밋**

```bash
git add coder_orchestration.py delegate_background.py tests/test_coder_orchestration_wake.py
git commit -m "feat(orchestration): wake main agent on coder completion via synthetic internal MessageEvent"
```

---

## Task 6: 도구 등록 + toolset membership + register(ctx) 배선

**Files:**
- Modify: `coder_orchestration.py` (`register_orchestration_tools`, `install_orchestration_toolset_membership`)
- Modify: `__init__.py` (`register`)
- Modify: `tests/test_coder_toolset_membership.py`
- Test: `tests/test_coder_orchestration_status.py`

두 도구를 registry에 등록하고 delegate_task를 가진 toolset에 추가해 메인 에이전트가 호출할 수 있게 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_coder_orchestration_status.py` 에 추가:

```python
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
```

> NOTE: registry의 단건 조회 API는 `registry.get_entry(name)` (→ Optional[ToolEntry]). 확인됨.

- [ ] **Step 2: 테스트 실패 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q -k register_and_membership`
Expected: FAIL — `AttributeError: module ... has no attribute 'register_orchestration_tools'`

- [ ] **Step 3: 최소 구현**

`coder_orchestration.py` 상단 import에 `json` 추가하고 registry import 보강 — import 블록을 다음으로 교체:

```python
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

from tools.delegate_tool import registry, check_delegate_requirements

from .delegate_background import (
    cancel_coder_run,
    claim_completion_notify,
    get_orchestration_run,
    list_orchestration_runs,
)
```

파일 **맨 끝**에 추가:

```python
CODER_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "coder_run_id": {
            "type": "string",
            "description": "특정 코더 런 ID. 생략하면 오케스트레이션 코더 전체 요약 + 용량.",
        },
        "include": {
            "type": "array",
            "items": {"type": "string", "enum": ["result", "log"]},
            "description": "상세 조회 시 결과 전문(result)/로그 tail(log)을 포함.",
        },
    },
}

CANCEL_CODER_SCHEMA = {
    "type": "object",
    "properties": {
        "coder_run_id": {
            "type": "string",
            "description": "취소할 오케스트레이션 코더의 런 ID.",
        },
    },
    "required": ["coder_run_id"],
}


def register_orchestration_tools() -> None:
    """coder_status / cancel_coder를 공유 registry에 등록(import 시 1회, 멱등)."""
    registry.register(
        name="coder_status",
        toolset="delegation",
        schema=CODER_STATUS_SCHEMA,
        handler=lambda args, **kw: json.dumps(
            coder_status(args.get("coder_run_id"), args.get("include")),
            ensure_ascii=False,
            default=str,
        ),
        check_fn=check_delegate_requirements,
        emoji="📋",
    )
    registry.register(
        name="cancel_coder",
        toolset="delegation",
        schema=CANCEL_CODER_SCHEMA,
        handler=lambda args, **kw: json.dumps(
            cancel_coder(args.get("coder_run_id")),
            ensure_ascii=False,
        ),
        check_fn=check_delegate_requirements,
        emoji="🛑",
    )


def install_orchestration_toolset_membership() -> None:
    """coder_status/cancel_coder를 delegate_task를 가진 모든 toolset에 추가.

    _install_coder_toolset_membership(delegate_task_background용)과 동일한 패턴:
    by-reference core는 in-place mutation으로, concatenation 복사본은 제네릭 스캔으로.
    """
    import toolsets

    new_tools = ("coder_status", "cancel_coder")
    core = toolsets._HERMES_CORE_TOOLS
    for name in new_tools:
        if name not in core:
            core.append(name)

    for ts in toolsets.TOOLSETS.values():
        tools = ts.get("tools")
        if tools is core or not isinstance(tools, list):
            continue
        if "delegate_task" in tools:
            for name in new_tools:
                if name not in tools:
                    tools.append(name)
```

`__init__.py` 의 `register(ctx)` 에서 `_install_coder_toolset_membership()` **직후**에 추가:

```python
    _install_coder_toolset_membership()
    from . import coder_orchestration
    coder_orchestration.register_orchestration_tools()
    coder_orchestration.install_orchestration_toolset_membership()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_orchestration_status.py -q -k register_and_membership`
Expected: PASS


- [ ] **Step 5: 기존 membership 테스트에 회귀 추가**

`tests/test_coder_toolset_membership.py` 의 `test_added_to_core_and_delegation` 아래에 추가:

```python
def test_orchestration_tools_added():
    from subagent_coder import coder_orchestration
    coder_orchestration.register_orchestration_tools()
    coder_orchestration.install_orchestration_toolset_membership()
    for name in ("coder_status", "cancel_coder"):
        assert name in toolsets._HERMES_CORE_TOOLS
        assert name in resolve_toolset("delegation")
        assert name in resolve_toolset("hermes-discord")
```

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/test_coder_toolset_membership.py -q`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add coder_orchestration.py __init__.py tests/test_coder_toolset_membership.py tests/test_coder_orchestration_status.py
git commit -m "feat(orchestration): register coder_status/cancel_coder tools + toolset membership + register(ctx) wiring"
```

---

## Task 7: 전체 회귀 + stock diff 0 무결성 검증

**Files:**
- Test: 전체 스위트 (신규 코드 변경 없음)

- [ ] **Step 1: 전체 스위트 통과 확인 (회귀 0)**

Run: `~/.venvs/hermes-stock-016/bin/python -m pytest tests/ -q`
Expected: PASS — 기존 48 + 신규(상태/취소/membership ~11 + 웨이크/캡처/라우팅 ~9) 전부 통과

- [ ] **Step 2: register(ctx) 무손상 임포트 확인 (in-process discover 시뮬)**

Run:
```bash
~/.venvs/hermes-stock-016/bin/python -c "
import sys; sys.argv = ['hermes']  # CLI 모드(게이트웨이 아님)
import importlib.util, pathlib
root = pathlib.Path('/mnt/e/ipynbs_port/legacy/nous_hermes/hermes-subagent-coder')
spec = importlib.util.spec_from_file_location('subagent_coder', root/'__init__.py', submodule_search_locations=[str(root)])
m = importlib.util.module_from_spec(spec); m.__package__='subagent_coder'; m.__path__=[str(root)]
sys.modules['subagent_coder']=m; spec.loader.exec_module(m)
from subagent_coder import coder_orchestration as o
print('coder_status empty:', o.coder_status())
print('OK import + tools')
"
```
Expected: `coder_status empty: {'active': 0, 'max': 3, 'available': 3, 'runs': []}` + `OK import + tools` (예외 없음)

- [ ] **Step 3: stock diff 0 확인**

Run:
```bash
cd /mnt/e/ipynbs_port/legacy/nous_hermes/hermes-subagent-coder
git diff --stat main..HEAD
```
Expected: 변경 파일은 전부 plugin 소유(`delegate_background.py`, `coder_orchestration.py`, `__init__.py`, `tests/*`, `docs/*`)뿐 — stock hermes 파일 없음.

- [ ] **Step 4: 설치본 동기화 (라이브 게이트웨이 반영 — 선택)**

> 라이브 스모크 전에만 필요. discover_plugins는 `~/.hermes/plugins/subagent_coder/` 설치본을 로드하므로 편집한 파일을 동기화:
```bash
cp delegate_background.py coder_orchestration.py __init__.py ~/.hermes/plugins/subagent_coder/
```

- [ ] **Step 5: 커밋(필요 시) + 푸시 준비**

```bash
git status   # 깨끗해야 함 (Task별로 이미 커밋됨)
```

---

## 라이브 스모크 (마지막, 수동 · secrets 필요 · 사용자 승인 후)

자동화 범위 밖. 게이트웨이(stock 0.16)에서:
1. Discord에서 **의존성 있는 작업** 지시 (예: "A 만들고, 되면 그걸 쓰는 B 만들어").
2. 메인이 독립 작업은 병렬(`delegate_task_background` ×N), 의존 작업은 직렬로 스케줄하는지.
3. 코더 완료 시 `[코더 ... 완료]` synthetic 메시지로 메인이 깨어나 다음 단계로 이어가는지.
4. `coder_status`로 용량 확인, `cancel_coder`로 중단 동작.
5. `~/.hermes/logs/agent.log`로 "메인 깨우기 주입" 체인 모니터.

---

## Self-Review (작성자 체크)

**스펙 커버리지:**
- §6.1 delegate_task_background(변경 없음) — 유지 ✓
- §6.2 coder_status — Task 3 ✓ (id 생략 요약+용량, id 지정 상세, include result/log, /code 제외)
- §6.3 cancel_coder — Task 4 ✓ (cancel_coder_run 래핑, 오케스트레이션만)
- §6.4 / §7 완료 알림 — Task 5 ✓ (synthetic 내부 MessageEvent, 성공/실패/취소 텍스트, 라우팅 source 재사용)
- §5 /code 분리 — main_source 단일 게이트로 Task 2~5 전반 ✓ (status/cancel/wake 모두 제외)
- §8 엣지: 중복방지(claim_completion_notify, Task 5 dedup 테스트) ✓ / cancelled 보호(Task 5 runner) ✓ / 라우팅·어댑터 분실 drop+경고(Task 5 _resolve_adapter/None 가드) ✓ / CLI no-op(gateway.run 부재 시 _resolve_adapter None) ✓
- §9 로그 캡처 — Task 1 (deque) + include=log tail(Task 3) ✓
- §3 stock diff 0 / 외부화 — Task 7 Step 3 ✓

**미해결 항목(스펙 §11) → 구현 중 확정:**
- `coder_status` 반환 필드/문자열 형태 — 본 계획대로 구현 후 라이브에서 메인 파싱 보며 조정.
- 완료 요약 문구 — `_build_completion_text` 톤 라이브 조정.
- log deque/tail 크기(200/20) — 라이브 토큰량 보며 조정.
- 라우팅 메타데이터 키/경로 — `main_source`(SessionSource 객체 그대로 재사용)·`main_loop`로 확정.

**타입 일관성:** `main_source`/`main_loop`/`notified`/`log` 레코드 키, `_LOG_MAXLEN`(200, db)·`_LOG_TAIL`(20, orch) 분리, `get_orchestration_run`/`list_orchestration_runs`/`claim_completion_notify`/`record_main_routing` 시그니처가 Task 간 일치 — 확인됨.

**API 확인 완료:** registry 단건 조회 = `registry.get_entry(name)`(확인). `gateway.platforms.base`에 `MessageEvent`(L1412)·`MessageType`(L1390, Enum) 존재 — `MessageType.TEXT` 확인. `SessionSource`는 스폰 시점 객체를 그대로 재사용(재구성 불필요).
