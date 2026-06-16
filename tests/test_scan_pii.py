"""scan_pii — redact 재사용 탐지·마스킹·toolset 멤버십 검증."""
from subagent_coder import scan_pii_tool as sp


def test_scan_pii_finds_email_and_masks(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text("contact me at hong.gildong@example.com please\n")
    out = sp.scan_pii(str(tmp_path))
    emails = [x for x in out["findings"] if x["type"] == "email"]
    assert emails, out
    # 원문 전체가 그대로 노출되면 안 됨(부분 마스킹).
    assert "hong.gildong@example.com" not in emails[0]["snippet"]


def test_scan_pii_finds_secret_via_redact(tmp_path):
    # redact.py가 아는 토큰 prefix(예: Google AIza...) — 라인이 redact로 변형됨.
    f = tmp_path / "cfg.txt"
    f.write_text('GOOGLE_KEY = "AIzaSyA1234567890abcdefghijklmnopqrstuv"\n')
    out = sp.scan_pii(str(tmp_path))
    assert any(x["type"] == "secret" for x in out["findings"]), out


def test_scan_pii_finds_home_path(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("saved to /home/realuser/secret/data.db\n")
    out = sp.scan_pii(str(tmp_path))
    assert any(x["type"] == "path" for x in out["findings"]), out


def test_scan_pii_clean_file_no_findings(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("def add(a, b):\n    return a + b\n")
    out = sp.scan_pii(str(tmp_path))
    assert out["count"] == 0
    assert "없음" in out.get("note", "")


def test_pii_toolset_registered_and_scan_pii_in_it():
    import toolsets
    sp.register_scan_pii_tool()
    sp.install_pii_toolset()
    assert "pii" in toolsets.TOOLSETS
    assert "scan_pii" in toolsets.TOOLSETS["pii"]["tools"]


def test_scan_pii_in_core_for_child_inheritance():
    """자식(reviewer)은 부모(메인)가 가진 toolset만 물려받는다(delegate_tool 규칙).
    메인이 scan_pii를 가져야 reviewer가 'pii' 요청 시 통과하므로 core에 등록."""
    import toolsets
    sp.install_pii_toolset()
    assert "scan_pii" in toolsets._HERMES_CORE_TOOLS


def test_scan_pii_added_to_delegation_toolset_for_main_visibility():
    """메인 self.tools는 enable한 toolset을 resolve해 채워진다. core 리스트 append만으론
    메인 도구에 안 잡히므로, 메인이 실제로 가진 toolset(delegate_task_background가 있는
    delegation 묶음)에 scan_pii를 끼워야 메인이 보유 → reviewer가 물려받는다.
    1차 coder_status(install_orchestration_toolset_membership)와 동일 패턴."""
    import toolsets
    sp.install_pii_toolset()
    deleg = [
        ts for ts in toolsets.TOOLSETS.values()
        if isinstance(ts.get("tools"), list) and "delegate_task" in ts["tools"]
    ]
    assert deleg, "delegate_task 가진 toolset(delegation)이 없음"
    assert all("scan_pii" in ts["tools"] for ts in deleg)
