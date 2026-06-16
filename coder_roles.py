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
}

DEFAULT_ROLE = "coder"


def get_role(name: Optional[str]) -> RoleConfig:
    """역할 설정 조회. 미지정/미지의 역할은 안전하게 coder로 폴백."""
    return ROLE_REGISTRY.get(name or DEFAULT_ROLE) or ROLE_REGISTRY[DEFAULT_ROLE]


def role_enum() -> List[str]:
    """스키마 enum용 유효 역할 목록."""
    return list(ROLE_REGISTRY.keys())
