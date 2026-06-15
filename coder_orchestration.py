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
import sys
from typing import Any, Dict, List, Optional

from .delegate_background import (
    cancel_coder_run,
    claim_completion_notify,
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
        tail = "\n".join(
            _format_log_line(e) for e in (snap.get("log") or [])[-_LOG_TAIL:]
        )
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
