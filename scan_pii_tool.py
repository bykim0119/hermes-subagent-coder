"""scan_pii — 결정론적 개인정보/비밀 스캐너.

stock ``agent/redact.py``(게이트웨이가 로그에서 비밀을 가릴 때 쓰는 검증된 엔진)를
재사용한다. 라인별로 ``redact_sensitive_text``를 돌려 "원문과 달라지면 비밀 발견"으로
판정하고, 순수 개인정보(이메일·실경로·IP)는 보강 패턴으로 잡는다. 모든 스니펫은
``mask_secret``으로 부분 마스킹해 도구 출력 자체로 개인정보가 새지 않게 한다.

reviewer 역할 전용 ``pii`` toolset으로 노출한다. 파일을 읽기만 하고 수정·실행하지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

# stock redact 엔진 재사용 (import만, 수정 없음 → stock diff 0).
from agent.redact import redact_sensitive_text, mask_secret
from tools.delegate_tool import registry, check_delegate_requirements

logger = logging.getLogger(__name__)

# redact가 다루지 않는 순수 개인정보 보강 패턴.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_HOME_PATH_RE = re.compile(r"(?:/home/|/Users/|C:\\Users\\)[^\s'\"/\\:]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_BOOSTERS = (("email", _EMAIL_RE), ("path", _HOME_PATH_RE), ("ip", _IPV4_RE))

# 스캔 제외 디렉터리/확장자(바이너리·VCS·캐시).
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"}
_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".so",
             ".pyc", ".bin", ".ico", ".woff", ".woff2"}
_MAX_BYTES = 2_000_000  # 2MB 초과 파일은 스킵(대용량/바이너리 방지).


def _iter_files(root: str):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in _SKIP_EXT:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > _MAX_BYTES:
                    continue
            except OSError:
                continue
            yield fp


def scan_pii(path: Optional[str] = None) -> Dict[str, Any]:
    """경로(기본=작업공간 전체)를 훑어 개인정보/비밀 발견 목록을 반환.

    각 발견: {file, line, type, snippet(부분 마스킹)}.
    type: "secret"(redact 엔진) / "email"·"path"·"ip"(보강 패턴).
    """
    root = path or "."
    findings: List[Dict[str, Any]] = []
    for fp in _iter_files(root):
        try:
            with open(fp, "r", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    # 1) redact 엔진 — 비밀/credential. 라인이 변형되면 발견.
                    red = redact_sensitive_text(line, force=True, code_file=True)
                    if red != line:
                        findings.append({
                            "file": fp, "line": lineno, "type": "secret",
                            "snippet": red.strip()[:200],
                        })
                    # 2) 보강 — 순수 개인정보.
                    for label, rx in _BOOSTERS:
                        for m in rx.finditer(line):
                            findings.append({
                                "file": fp, "line": lineno, "type": label,
                                "snippet": mask_secret(m.group(0)),
                            })
        except (OSError, UnicodeError):
            continue
    if not findings:
        return {"findings": [], "count": 0, "note": "개인정보 패턴 없음"}
    return {"findings": findings, "count": len(findings)}


SCAN_PII_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "스캔할 파일/폴더 경로. 생략 시 작업공간 전체를 훑는다. "
                "공개·제출할 결과물의 경로를 지정하면 그 범위만 점검한다."
            ),
        },
    },
    "required": [],
}


def register_scan_pii_tool() -> None:
    """scan_pii를 공유 registry에 등록(import 시 1회, 멱등)."""
    registry.register(
        name="scan_pii",
        toolset="pii",
        schema=SCAN_PII_SCHEMA,
        description=(
            "결과물에서 개인정보/비밀(이메일·토큰·키·IP·실경로·ID 등)을 결정론적으로 "
            "스캔해 발견 위치 목록을 반환한다(값은 부분 마스킹). 공개·제출 직전 점검용. "
            "정형 항목 위주이며, 실명 등 비정형은 직접 읽어 추론으로 보완하라."
        ),
        handler=lambda args, **kw: json.dumps(
            scan_pii(args.get("path")), ensure_ascii=False, default=str
        ),
        check_fn=check_delegate_requirements,
        emoji="🔍",
    )


def install_pii_toolset() -> None:
    """``pii`` toolset 정의 + scan_pii를 core에 등록(멱등).

    hermes의 자식 toolset 규칙(delegate_tool.py: ``child_toolsets = [t for t in
    toolsets if t in expanded_parent]``)은 **부모(메인)가 가진 toolset만** 자식에게
    물려준다. 메인이 scan_pii를 가져야 ``_expand_parent_toolsets``가 'pii'를 부모
    toolset으로 인식해 reviewer(자식)의 toolsets=("file","pii") 요청이 통과한다.
    그래서 scan_pii를 _HERMES_CORE_TOOLS에 등록한다(1차 coder_status와 동일 패턴).
    다른 역할도 도구를 *볼 수는* 있으나, 안내문상 reviewer만 사용한다.
    """
    import toolsets

    ts = toolsets.TOOLSETS.get("pii")
    if ts is None:
        toolsets.TOOLSETS["pii"] = {
            "description": "개인정보/비밀 스캔 (reviewer 역할용)",
            "tools": ["scan_pii"],
            "includes": [],
        }
    elif "scan_pii" not in ts.get("tools", []):
        ts["tools"].append("scan_pii")

    core = toolsets._HERMES_CORE_TOOLS
    if "scan_pii" not in core:
        core.append("scan_pii")


register_scan_pii_tool()
