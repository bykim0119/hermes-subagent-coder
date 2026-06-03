"""OpenAI Codex CLI provider profile.

codex-exec runs an external `codex exec --json` subprocess that drives an
internal agent loop and emits NDJSON events. The CodexExecFacade
(plugins.subagent_coder.codex_exec_client.py) wraps it in an OpenAI chat-completion shape so
hermes' AIAgent can treat the whole Codex turn as a single LLM call.
"""

from providers import register_provider
from providers.base import ProviderProfile


class CodexExecProfile(ProviderProfile):
    """Codex CLI — external process, no REST models endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Model listing is handled by the Codex CLI subprocess."""
        return None


codex_exec = CodexExecProfile(
    name="codex-exec",
    aliases=(),
    api_mode="chat_completions",
    env_vars=(),  # Managed by the Codex CLI subprocess (~/.codex/auth.json)
    base_url="codex-exec://local",
    auth_type="external_process",
)

def register_codex_provider(ctx) -> None:
    """subagent_coder.register(ctx)에서 호출 — codex-exec provider를 전역 등록.

    provider 등록은 ``providers`` 전역 레지스트리(``register_provider``)를 통하므로
    ``ctx``는 사용하지 않는다. 과거에는 module import 부작용으로 등록됐으나
    (별도 ``model-providers/codex-exec`` plugin), 이제 코더 관련 wiring 전부를
    subagent_coder의 단일 ``register(ctx)`` 진입점으로 모은다.
    """
    register_provider(codex_exec)
