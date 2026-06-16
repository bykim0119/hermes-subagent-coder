"""역할 fleet 정의 — 한 역할 = {모델, 도구, 안내문} 설정.

배경 서브에이전트의 역할별 설정을 한 곳에 모은 단일 진실원. delegate_background가
이 설정으로 스폰을 분기하고, coder_status가 런의 역할을 표시한다. 새 역할 추가 =
ROLE_REGISTRY에 한 줄. (stock delegate_task의 role="leaf"/"orchestrator"와는 다른,
fleet 차원의 역할 개념이다.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class RoleConfig:
    name: str
    provider: Optional[str]        # "codex-exec"=코더 경로; None=메인 추론모델 상속
    toolsets: Tuple[str, ...]      # 자식에게 줄 toolset 이름들
    instructions: str              # 역할 안내문(비-codex 역할에서 goal 앞에 주입)
    use_when: str                  # 스키마 enum 설명(모델 선택 안내)
    result_label: str              # 완료 웨이크 라벨(예: "코더"/"플래너")
    completion_suffix: str = ""     # 완료 웨이크 끝에 붙는 역할별 지시


_PLANNER_INSTRUCTIONS = (
    "너는 설계자(planner)다. 주어진 목표를 위해 코드베이스를 조사하고, "
    "구체적인 구현 계획서를 작성하라. 계획서는 docs/superpowers/plans/ 아래에 "
    "마크다운 파일로 저장하라(write_file 사용). 프로젝트 코드 자체는 절대 수정하지 "
    "마라 — 너의 산출물은 '계획서 문서' 하나다. 끝에 계획서 파일 경로와 한 줄 요약을 "
    "보고하라."
)

_TESTER_INSTRUCTIONS = (
    "너는 테스터(QA)다. 구현된 코드의 테스트를 작성하고 실행해 검증하라. "
    "프로덕션 코드 변경은 최소화하고 테스트 코드 위주로 작업하라. "
    "끝에 통과/실패 여부와 핵심 로그를 보고하라."
)

_REVIEWER_INSTRUCTIONS = (
    "너는 리뷰어다. 코드·결과물을 비평하고, 보스에게 공개·제출하기 전 개인정보를 "
    "점검하라. 절차: ① 먼저 scan_pii 도구로 정형 개인정보(이메일·토큰·키·IP·실경로·ID 등)를 "
    "훑고 → ② 그다음 파일을 읽어 비정형 개인정보(실명·내부 호스트명)를 추론으로 보완하라. "
    "코드는 절대 수정하지 마라(읽고 비평만). 발견 항목은 위치와 함께 목록화하라. "
    "끝에 비평 요약과 개인정보 발견 목록을 보고하라."
)

ROLE_REGISTRY = {
    "coder": RoleConfig(
        name="coder",
        provider="codex-exec",
        toolsets=("terminal", "file"),
        instructions="",
        use_when="범위가 분명한 구현·수정 작업",
        result_label="코더",
        completion_suffix="",
    ),
    "planner": RoleConfig(
        name="planner",
        provider=None,
        toolsets=("file",),
        instructions=_PLANNER_INSTRUCTIONS,
        use_when="크거나 익숙지 않아 먼저 조사·설계가 필요한 작업",
        result_label="플래너",
        completion_suffix=(
            "\n→ 이 계획을 보스에게 보여주고 승인받은 뒤 구현을 코더에게 위임하라 "
            "(보스가 자율을 지시했으면 바로 진행)."
        ),
    ),
    "tester": RoleConfig(
        name="tester",
        provider="codex-exec",
        toolsets=("terminal", "file"),
        instructions=_TESTER_INSTRUCTIONS,
        use_when="구현된 코드의 테스트 작성·실행 검증",
        result_label="테스터",
        completion_suffix=(
            "\n→ 테스트가 실패했으면 원인을 코더에게 수정 위임하거나 보스에게 보고하라."
        ),
    ),
    "reviewer": RoleConfig(
        name="reviewer",
        provider=None,
        toolsets=("file", "pii"),
        instructions=_REVIEWER_INSTRUCTIONS,
        use_when="결과물 품질 리뷰, 공개·제출 직전 개인정보 점검",
        result_label="리뷰어",
        completion_suffix=(
            "\n→ 개인정보나 중대한 문제가 있으면 공개를 보류하고 보스에게 보고하라"
            "(체크포인트). 없으면 통과로 보고하라."
        ),
    ),
}

DEFAULT_ROLE = "coder"


def get_role(name: Optional[str]) -> RoleConfig:
    """역할 설정 조회. 미지정/미지의 역할은 안전하게 coder로 폴백."""
    return ROLE_REGISTRY.get(name or DEFAULT_ROLE) or ROLE_REGISTRY[DEFAULT_ROLE]


def role_enum() -> List[str]:
    """스키마 enum용 유효 역할 목록."""
    return list(ROLE_REGISTRY.keys())
