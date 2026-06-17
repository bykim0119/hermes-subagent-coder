"""Skeleton: plugin은 발견되고 register(ctx)가 callable이어야 한다."""
import importlib


def test_agent_company_module_importable():
    mod = importlib.import_module("agent_company")
    assert hasattr(mod, "register"), "register entry point 누락"
    assert callable(mod.register)


def test_plugin_yaml_exists_with_required_fields():
    from pathlib import Path

    import yaml

    p = Path(__file__).resolve().parents[1] / "plugin.yaml"
    assert p.exists(), f"plugin.yaml not found at {p}"
    data = yaml.safe_load(p.read_text())
    assert data.get("name") == "agent_company"
    # label/description: Task 0.2 결과 — label 필드 지원 확인됨
    assert data.get("label")
    assert data.get("description")
