"""S6 Discord coder overlay 검증.

stock gateway/platforms/discord.py를 안 건드리고(코더 코드 0), register(ctx)의
``install_discord_coder_overlay()``가 DiscordAdapter에:
  * 코더 헬퍼 메서드 7개를 setattr로 부착
  * __init__/_run_post_connect_initialization/disconnect/_handle_message/
    _register_slash_commands를 wrap
하는지 확인. DiscordAdapter는 fake 클래스로 POC 한다(실제 discord.py 어댑터는
무겁고 gateway 전용).
"""
import asyncio
import sys
import types

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from subagent_coder import _install_discord_coder_overlay
from subagent_coder import discord_overlay


def _is_adapter_module(name):
    return (
        name.endswith("discord_platform.adapter")
        or name.endswith("platforms.discord.adapter")
        or name == "gateway.platforms.discord"
    )


@pytest.fixture(autouse=True)
def _isolate_discord_adapter_modules():
    """Strip any real/leaked Discord adapter modules from sys.modules so the
    resolver only sees the fake each test installs.

    Other test files run ``discover_plugins`` which loads the bundled adapter as
    ``hermes_plugins.discord_platform.adapter``; left in sys.modules it would be
    picked over a test's ``gateway.platforms.discord`` fake (cross-file pollution).
    """
    saved = {n: sys.modules[n] for n in list(sys.modules) if _is_adapter_module(n)}
    for n in saved:
        del sys.modules[n]
    yield
    for n, m in saved.items():
        sys.modules[n] = m


_CODER_METHODS = (
    "_make_thread_name",
    "_publish_to_thread",
    "create_coder_thread",
    "on_coder_event",
    "_handle_code_slash",
    "_cancel_coder_run",
    "_handle_coder_followup",
)


def _make_stub_adapter_cls():
    """매 테스트 fresh 클래스 — install이 클래스 sentinel로 idempotent라
    클래스를 새로 만들어야 wrap이 다시 적용된다."""

    class DiscordAdapter:  # name matches real class (retrofit matches by name)
        def __init__(self, *args, **kwargs):
            self.name = "discord"
            self._client = None
            self.init_called = True

        async def _run_post_connect_initialization(self):
            self.post_called = True
            return "orig_post"

        async def disconnect(self):
            self.disconnect_called = True
            return "orig_disc"

        async def _handle_message(self, message):
            self.handled = message
            return "orig_handle"

        def _register_slash_commands(self):
            self.slash_called = True
            return "orig_slash"

    return DiscordAdapter


def _install_on(cls, monkeypatch):
    """fake gateway.platforms.discord 모듈에 cls를 DiscordAdapter로 노출 후 install."""
    fake_mod = types.ModuleType("gateway.platforms.discord")
    fake_mod.DiscordAdapter = cls
    monkeypatch.setitem(sys.modules, "gateway.platforms.discord", fake_mod)
    discord_overlay.install_discord_coder_overlay()
    return cls


# --- CLI 모드 / 가드 ----------------------------------------------------------

def test_install_noop_in_cli_mode(monkeypatch):
    """gateway 신호가 전혀 없으면 _install_discord_coder_overlay no-op."""
    monkeypatch.delitem(sys.modules, "gateway.platforms.discord", raising=False)
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    # 예외 없이 통과 + overlay 설치 함수 미호출
    with patch.object(
        discord_overlay, "install_discord_coder_overlay"
    ) as install:
        _install_discord_coder_overlay()
    install.assert_not_called()


def test_install_proceeds_when_only_argv_signals_gateway(monkeypatch):
    """회귀: 이른 plugin discovery는 gateway.run import 전에 실행될 수 있다.

    그때 sys.modules엔 gateway.run/gateway.platforms.discord가 둘 다 없어
    sys.modules만 보면 'CLI 모드'로 오판하고 overlay를 통째로 건너뛴다
    (그 결과 /code 슬래시 + 코더 Discord 기능 전부 누락). 실행 명령
    ``hermes gateway run``의 sys.argv 신호로 gateway 모드를 잡아야 한다.
    """
    monkeypatch.delitem(sys.modules, "gateway.platforms.discord", raising=False)
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run", "--replace"])
    with patch.object(
        discord_overlay, "install_discord_coder_overlay"
    ) as install:
        _install_discord_coder_overlay()
    install.assert_called_once()


def test_overlay_install_noop_when_adapter_missing(monkeypatch):
    """모듈은 있으나 DiscordAdapter 심볼이 없으면 no-op."""
    fake_mod = types.ModuleType("gateway.platforms.discord")
    monkeypatch.setitem(sys.modules, "gateway.platforms.discord", fake_mod)
    discord_overlay.install_discord_coder_overlay()  # 예외 없음


# --- setattr 메서드 + sentinel -----------------------------------------------

def test_overlay_attaches_coder_methods(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    for name in _CODER_METHODS:
        assert callable(getattr(cls, name, None)), f"{name} not attached"
    assert getattr(cls, "_subagent_coder_overlay_installed", False) is True


def test_overlay_install_via_hermes_016_module_path(monkeypatch):
    """hermes >=0.16 moved Discord to ``plugins.platforms.discord.adapter``.

    The overlay must find ``DiscordAdapter`` at the new path (and the legacy
    ``gateway.platforms.discord`` must be absent for this to be meaningful).
    """
    monkeypatch.delitem(sys.modules, "gateway.platforms.discord", raising=False)
    cls = _make_stub_adapter_cls()
    fake_mod = types.ModuleType("plugins.platforms.discord.adapter")
    fake_mod.DiscordAdapter = cls
    monkeypatch.setitem(sys.modules, "plugins.platforms.discord.adapter", fake_mod)
    discord_overlay.install_discord_coder_overlay()
    for name in _CODER_METHODS:
        assert callable(getattr(cls, name, None)), f"{name} not attached (0.16 path)"
    assert getattr(cls, "_subagent_coder_overlay_installed", False) is True


def test_overlay_wraps_plugin_namespaced_live_adapter(monkeypatch):
    """hermes >=0.16 loads the bundled Discord platform as a plugin, so the
    *live* adapter class lives in ``hermes_plugins.discord_platform.adapter`` —
    not the on-disk ``plugins.platforms.discord.adapter`` (importing that path
    yields a second, unused class). The overlay must wrap the plugin-namespaced
    live class, otherwise /code + coder routing never reach the running adapter.
    """
    monkeypatch.delitem(sys.modules, "gateway.platforms.discord", raising=False)
    live_cls = _make_stub_adapter_cls()   # the one the gateway instantiates
    dead_cls = _make_stub_adapter_cls()   # on-disk import — must stay untouched

    live_mod = types.ModuleType("hermes_plugins.discord_platform.adapter")
    live_mod.DiscordAdapter = live_cls
    dead_mod = types.ModuleType("plugins.platforms.discord.adapter")
    dead_mod.DiscordAdapter = dead_cls
    monkeypatch.setitem(sys.modules, "hermes_plugins.discord_platform.adapter", live_mod)
    monkeypatch.setitem(sys.modules, "plugins.platforms.discord.adapter", dead_mod)

    discord_overlay.install_discord_coder_overlay()

    assert getattr(live_cls, "_subagent_coder_overlay_installed", False) is True, \
        "live (plugin-namespaced) adapter was not wrapped"
    assert callable(getattr(live_cls, "create_coder_thread", None))
    assert getattr(dead_cls, "_subagent_coder_overlay_installed", False) is False, \
        "on-disk adapter should be left untouched"


def test_overlay_idempotent(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    wrapped_init = cls.__init__
    # 두 번째 install은 sentinel로 skip → __init__ 재-wrap 안 됨
    discord_overlay.install_discord_coder_overlay()
    assert cls.__init__ is wrapped_init


# --- __init__ wrap -----------------------------------------------------------

def test_init_wrap_creates_coder_sessions(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    assert adapter.init_called is True  # orig __init__ 실행됨
    from subagent_coder.coder_sessions import CoderSessionManager
    assert isinstance(adapter._coder_sessions, CoderSessionManager)
    assert adapter._coder_flusher is None


# --- _handle_message wrap (코더 thread 라우팅) --------------------------------

def _make_thread_channel(monkeypatch):
    """discord.Thread isinstance 통과하는 fake 채널 + 모듈 discord 치환."""
    class _FakeThread:
        def __init__(self):
            self.id = 777

    fake_discord = types.SimpleNamespace(Thread=_FakeThread)
    monkeypatch.setattr(discord_overlay, "discord", fake_discord)
    return _FakeThread()


def test_handle_message_routes_followup(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._coder_sessions = MagicMock()
    adapter._coder_sessions.get_coder_by_thread.return_value = "coder-1"
    adapter._handle_coder_followup = AsyncMock()
    adapter._cancel_coder_run = AsyncMock()

    channel = _make_thread_channel(monkeypatch)
    message = MagicMock()
    message.channel = channel
    message.content = "keep going please"

    with patch(
        "subagent_coder.delegate_background.is_cancel_command",
        return_value=False,
    ):
        asyncio.run(adapter._handle_message(message))

    adapter._handle_coder_followup.assert_awaited_once_with(
        "coder-1", "keep going please", channel
    )
    adapter._cancel_coder_run.assert_not_awaited()
    assert not hasattr(adapter, "handled")  # orig _handle_message 미호출
    adapter._coder_sessions.touch.assert_called_once_with("coder-1")


def test_handle_message_routes_cancel(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._coder_sessions = MagicMock()
    adapter._coder_sessions.get_coder_by_thread.return_value = "coder-9"
    adapter._handle_coder_followup = AsyncMock()
    adapter._cancel_coder_run = AsyncMock()

    channel = _make_thread_channel(monkeypatch)
    message = MagicMock()
    message.channel = channel
    message.content = "stop"

    with patch(
        "subagent_coder.delegate_background.is_cancel_command",
        return_value=True,
    ):
        asyncio.run(adapter._handle_message(message))

    adapter._cancel_coder_run.assert_awaited_once_with("coder-9", channel)
    adapter._handle_coder_followup.assert_not_awaited()
    assert not hasattr(adapter, "handled")


def test_handle_message_falls_through_non_coder_thread(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._coder_sessions = MagicMock()
    # thread이지만 바인딩된 코더 없음
    adapter._coder_sessions.get_coder_by_thread.return_value = None

    channel = _make_thread_channel(monkeypatch)
    message = MagicMock()
    message.channel = channel
    message.content = "hi hermes"

    result = asyncio.run(adapter._handle_message(message))
    assert result == "orig_handle"
    assert adapter.handled is message  # orig 실행됨


def test_handle_message_falls_through_non_thread(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._coder_sessions = MagicMock()

    _make_thread_channel(monkeypatch)  # discord.Thread 치환
    message = MagicMock()
    message.channel = object()  # Thread 아님
    message.content = "hi"

    result = asyncio.run(adapter._handle_message(message))
    assert result == "orig_handle"
    assert adapter.handled is message
    adapter._coder_sessions.get_coder_by_thread.assert_not_called()


# --- _register_slash_commands wrap (/code) -----------------------------------

def test_register_slash_adds_code_command(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._client = MagicMock()
    adapter._client.tree.get_command.return_value = None  # /code not yet on tree

    result = adapter._register_slash_commands()
    assert result == "orig_slash"
    assert adapter.slash_called is True  # orig 실행됨
    # /code 가 tree에 등록됨
    cmd_calls = [c.kwargs for c in adapter._client.tree.command.call_args_list]
    assert any(c.get("name") == "code" for c in cmd_calls)


def test_register_slash_noop_without_client(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._client = None
    # _client 없으면 /code 등록 시도 안 함(예외 없이 orig 결과 반환)
    assert adapter._register_slash_commands() == "orig_slash"


# --- disconnect wrap ---------------------------------------------------------

def test_disconnect_unregisters_bus(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()
    adapter._coder_sessions = MagicMock()

    with patch(
        "subagent_coder.coder_event_bus.unregister_handler"
    ) as mock_unreg, patch(
        "subagent_coder.coder_sessions.get_global_sessions",
        return_value=None,
    ):
        result = asyncio.run(adapter.disconnect())

    assert result == "orig_disc"
    assert adapter.disconnect_called is True  # orig 실행됨
    mock_unreg.assert_called_once_with(adapter.on_coder_event)


# --- _run_post_connect_initialization wrap -----------------------------------

def test_post_connect_starts_flusher_and_registers(monkeypatch):
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)
    adapter = cls()  # __init__ wrap → _coder_flusher None

    fake_flusher = MagicMock()
    with patch(
        "subagent_coder.coder_progress_formatter.DebouncedFlusher",
        return_value=fake_flusher,
    ), patch(
        "subagent_coder.coder_event_bus.register_handler"
    ) as mock_reg, patch(
        "subagent_coder.coder_sessions.set_global_sessions"
    ):
        result = asyncio.run(adapter._run_post_connect_initialization())

    assert result == "orig_post"
    assert adapter.post_called is True  # orig 실행됨
    assert adapter._coder_flusher is fake_flusher
    fake_flusher.start.assert_called_once()
    mock_reg.assert_called_once()
    # register_handler 첫 인자 = adapter.on_coder_event
    assert mock_reg.call_args.args[0] == adapter.on_coder_event


# --- late retrofit (discovery ran after adapter connected) -------------------

def test_retrofit_wires_live_adapter_state(monkeypatch):
    """overlay가 어댑터 connect 이후 설치될 때, live 인스턴스에 코더 상태를
    retrofit 하는지. (loop 미실행 = connect 전 → state만 주입하고 종료)"""
    cls = _install_on(_make_stub_adapter_cls(), monkeypatch)

    # __init__ 거치지 않은 '오버레이 이전' 인스턴스 시뮬
    adapter = cls.__new__(cls)
    adapter.name = "discord"
    adapter._client = None  # 아직 connect 전 → loop 없음

    runner = MagicMock()
    runner.adapters = {"discord": adapter}
    fake_gw = types.ModuleType("gateway.run")
    fake_gw._gateway_runner_ref = lambda: runner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gw)

    discord_overlay._retrofit_live_discord_adapter()

    from subagent_coder.coder_sessions import CoderSessionManager
    assert isinstance(adapter._coder_sessions, CoderSessionManager)
    assert adapter._coder_flusher is None
    assert adapter._subagent_coder_retrofitted is True


def test_retrofit_noop_without_runner(monkeypatch):
    """gateway.run 미로드/ runner 없음이면 retrofit no-op (예외 없음)."""
    _install_on(_make_stub_adapter_cls(), monkeypatch)
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    discord_overlay._retrofit_live_discord_adapter()  # 예외 없이 통과
