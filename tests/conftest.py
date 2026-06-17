"""Test bootstrap for the standalone agent_company plugin repo.

Repo root *is* the plugin package (flat layout required by ``hermes plugins
install``). Two jobs:
  1. Load the flat root package as ``agent_company`` so tests can import it.
  2. Stop pytest from importing the root ``__init__.py`` as a stray package
     node (it can't — the package is meant to be loaded by name, and the repo
     dir name isn't a valid Python identifier).
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_NAME = "agent_company"

if _NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _NAME, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _NAME
    module.__path__ = [str(_ROOT)]
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)


def pytest_ignore_collect(collection_path, config):
    # Never collect the plugin entry point or repo root as a test package.
    p = Path(str(collection_path))
    if p.name == "__init__.py" and p.parent == _ROOT:
        return True
    if p == _ROOT:
        return True
    return None
