"""Configuration helpers for the codex coder subagent.

Two responsibilities:

* ``coder_setting(key, env_var=..., default=..., cast=...)`` — resolves
  ``delegation.coder.<key>`` with priority ``env > config > default``.
  Used by ``CoderSessionManager`` init, ``DebouncedFlusher`` interval,
  and the codex CLI argv resolver in ``tools.delegate_tool``.

* ``check_codex_auth()`` — pre-spawn check that surfaces missing/expired
  ``~/.codex/auth.json`` as a fast user-facing error, instead of letting
  codex fail mid-NDJSON-stream where the user only sees ``error:
  returncode=N`` in their Discord thread.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, TypeVar

# Module-level import so tests can ``patch("plugins.agent_company.config.load_config", ...)``
# instead of patching the absolute path. Failure to import (e.g. standalone
# pytest run) is tolerated — we just behave as if config is empty.
try:
    from hermes_cli.config import load_config
except Exception:  # pragma: no cover - import-time guard only
    def load_config() -> dict:
        return {}

logger = logging.getLogger(__name__)

T = TypeVar("T")


def coder_setting(
    key: str,
    *,
    env_var: Optional[str],
    default: T,
    cast: Callable[[Any], T] = lambda x: x,
) -> T:
    """Resolve ``delegation.coder.<key>`` with ``env > config > default``.

    Empty env strings count as unset, matching shell ``${X:-fallback}``
    semantics — an operator who clears a variable in a systemd drop-in
    expects the config to win, not the (still-set, just empty) env.

    ``cast`` failures are logged and skipped rather than raised; a typo in
    operator-supplied env shouldn't take the bot down.
    """
    if env_var:
        raw = os.environ.get(env_var)
        if raw not in (None, ""):
            try:
                return cast(raw)
            except Exception:
                logger.warning(
                    "Invalid env %s=%r — falling back to config/default for %s",
                    env_var, raw, key,
                )

    try:
        cfg = load_config() or {}
        node: Any = (cfg.get("delegation") or {}).get("coder") or {}
        if key in node:
            try:
                return cast(node[key])
            except Exception:
                logger.warning(
                    "Invalid config delegation.coder.%s=%r — using default",
                    key, node[key],
                )
    except Exception:
        logger.debug("coder_setting: load_config raised", exc_info=True)

    return default


def check_codex_auth() -> Optional[str]:
    """Return None if codex auth looks usable, otherwise a user-facing error.

    A missing file is a definite "fix this first" so it gets a specific
    suggestion. A malformed file is suspicious but codex's own error
    surface is richer — we defer rather than misdiagnose.
    """
    auth_path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(auth_path):
        return "Codex auth.json 없음 — `codex login` 또는 ChatGPT OAuth 재설정 필요"

    try:
        import json
        import time as _time
        from datetime import datetime
        with open(auth_path, encoding="utf-8") as f:
            auth = json.load(f)
        # Codex auth.json schema varies — tolerate top-level or nested
        # "tokens" placement. Absent expiry isn't a failure: some auth modes
        # don't carry an expiration timestamp.
        exp = auth.get("expires_at")
        if not exp:
            tokens = auth.get("tokens") or {}
            exp = tokens.get("expires_at") or tokens.get("access_token_expires_at")
        if exp:
            ts = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
            if _time.time() > ts:
                return "Codex OAuth 만료 — `codex login` 재실행 필요"
    except Exception:
        logger.debug("check_codex_auth: parse failed, deferring to codex", exc_info=True)

    return None
