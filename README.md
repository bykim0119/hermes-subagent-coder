# hermes-subagent-coder

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that adds a
**background coder sub-agent** powered by the [Codex CLI](https://github.com/openai/codex).
Hermes delegates a coding task to a `codex exec` subprocess running in a daemon
thread and streams live progress back to the chat UI — Discord today, with a
platform-agnostic core.

It installs entirely as a plugin: **no fork of hermes required**. Every hook into
stock hermes is applied at `register(ctx)` time via runtime wraps / monkey-patches,
so a stock `pip install`ed hermes stays byte-for-byte unmodified.

## What it adds

- **`delegate_task_background` tool** — the LLM hands a coding task to a detached
  coder run instead of blocking the main turn.
- **`codex-exec` model provider** — wraps the Codex CLI in an OpenAI
  chat-completion shape.
- **Discord overlay**
  - `/code <task>` slash command — deterministic coder spawn (bypasses LLM tool
    selection).
  - A dedicated thread per coder run with live, debounced progress.
  - Follow-up messages in a coder thread resume the same `codex exec` session;
    `stop`/`cancel` terminates the run.

## Requirements

- Hermes (`hermes-agent`) installed.
- The `codex` CLI on `PATH`.
- Codex auth at `~/.codex/auth.json` (run `codex login`).
- For the Discord features: a Discord bot token configured in hermes
  (`DISCORD_BOT_TOKEN`) and `discord.py` (a hermes messaging dependency).

## Install

```bash
hermes plugins install bykim0119/hermes-subagent-coder
hermes plugins enable subagent_coder
```

This clones the repo into `~/.hermes/plugins/subagent_coder/`. hermes discovers it
on next start and runs its `register(ctx)`.

To update or remove:

```bash
hermes plugins update subagent_coder
hermes plugins remove subagent_coder
```

## Usage

- **Slash command:** `/code add a /health endpoint and a test for it`
- **Natural language:** ask hermes to do a coding task; it will pick
  `delegate_task_background` and open a coder thread.
- **Follow up:** just reply in the coder thread — it resumes the same session.
- **Cancel:** send `stop` (or `cancel`) in the thread.

## Configuration

Tunables resolve in priority order **env var → `delegation.coder.<key>` in
hermes `config.yaml` → default**:

| Setting | Env var | Default |
|---|---|---|
| Idle session timeout (s) | `HERMES_CODER_IDLE_TIMEOUT_S` | `7200` |
| Max concurrent coder runs | `HERMES_CODER_MAX_CONCURRENT` | `3` |
| Progress debounce (ms) | `HERMES_CODER_DEBOUNCE_MS` | `250` |
| Allow Discord DMs | `DISCORD_ALLOW_DMS` | `true` |

## How it stays fork-free

`register(ctx)` is the single wiring entry point. It:

- registers the `delegate_task_background` tool and the `codex-exec` provider,
- wraps `AIAgent._invoke_tool` / `_execute_tool_calls_sequential` to inject
  `parent_agent` (stock hermes doesn't pass it through the registry dispatch),
- wraps `_build_child_agent` / `_build_child_progress_callback` to pin the coder
  child's id and provider,
- installs the per-turn `coder_spawn_callback` and Discord thread routing by
  wrapping the gateway runner and `DiscordAdapter` methods,
- adds `delegate_task_background` to the relevant toolsets,
- resolves `codex-exec` credentials.

All wraps are idempotent and guard on the relevant module being loaded, so in CLI
mode (no gateway / no Discord) the platform wires are no-ops.

## Development

The package is a **flat layout** — `__init__.py` and its sibling modules live at
the repo root, because `hermes plugins install` loads the plugin from the repo
root of the clone. The tests load that flat package as `subagent_coder` (see
`tests/conftest.py`).

The tests import and monkey-patch stock hermes modules (`run_agent`, `tools.*`,
`gateway.*`), so hermes must be importable:

```bash
pip install -e ".[dev]"   # or otherwise have hermes-agent importable
pytest
```

## License

See the upstream hermes-agent project.
