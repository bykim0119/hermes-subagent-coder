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
- **Orchestration tools** — `coder_status` (inspect running/finished coder runs
  and remaining capacity) and `cancel_coder` (cancel a run programmatically).
  Scope is limited to agent-spawned runs; `/code` slash runs are excluded.
- **Completion wake** — when an agent-spawned coder finishes, a synthetic
  internal message is injected into the main session so the agent wakes on a new
  turn and can schedule dependent follow-ups. Mirrors hermes's stock
  background-process completion notification, so **no polling is required**.
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

## Orchestrating multiple coders

For a multi-task job, the main agent can run several coders and chain dependent
work:

- Fire **independent** tasks in parallel — call `delegate_task_background`
  multiple times in one turn.
- A **dependent** task is delegated only after the completion wake(s) it depends
  on arrive: the agent ends its turn, gets woken when each coder finishes, then
  delegates the next step.
- `coder_status` reports active runs and remaining capacity (capacity is
  `HERMES_CODER_MAX_CONCURRENT`); `cancel_coder` stops a run that's going the
  wrong way.

### The mechanism vs. the agent's role

The plugin only provides the **mechanism** (spawn / observe / cancel / wake). It
does **not** decide *what* to parallelize or whether the agent writes code
itself — that is governed by the agent's **persona / system prompt**, which lives
in your hermes config (`~/.hermes/SOUL.md` or your equivalent system-prompt
source), **not in this plugin**.

In practice the agent will only behave as a disciplined orchestrator if its
persona says so. Without role framing it tends to (a) poll `coder_status` inside
one turn instead of relying on the wake, and (b) do small integration steps
itself instead of delegating them. Add an orchestrator block to the persona, e.g.:

```text
When given coding work, you are the project orchestrator:
- Do NOT write code yourself. Delegate every coding task — including small
  integration/glue steps — via delegate_task_background.
- Split the request into independent vs dependent tasks. Fire independent tasks
  in parallel (several calls in one turn); delegate a dependent task only after
  the completion notification(s) it depends on arrive.
- After delegating, END your turn and wait — completions are delivered to you
  automatically. Do not poll coder_status in a loop; call it only when the user
  asks for progress or to check capacity before a new delegation.
- When coders finish, collect and verify their results, then report back.
```

> Note: the persona file is part of your hermes setup, not this repo. Updating
> the plugin (`hermes plugins update`) does not change it — keep your persona
> under your own backup.

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

- registers the `delegate_task_background`, `coder_status`, and `cancel_coder`
  tools and the `codex-exec` provider,
- records the main session's routing on each agent-spawned run and injects the
  completion wake when the run finishes,
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
