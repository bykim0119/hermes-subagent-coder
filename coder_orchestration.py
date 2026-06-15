"""코더 오케스트레이션 도구 — 메인 에이전트의 코더 관찰·제어·완료알림.

agent-spawn된 코더("오케스트레이션 런")에 대한 메인 에이전트의 "시야"를 외부화한다:
  * ``coder_status`` (read)  — 오케스트레이션 코더 목록/상세 + 용량 조회.
  * ``cancel_coder`` (action) — 오케스트레이션 코더를 프로그래밍적으로 취소.
  * 완료 웨이크              — 코더 완료 시 메인 세션에 synthetic 내부 MessageEvent를
                               주입. stock 백그라운드-프로세스 알림 미러.

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
