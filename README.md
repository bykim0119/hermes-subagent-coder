# hermes-agent-company

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that turns
background delegation into a small **company of role-specialized sub-agents** —
*planner, coder, tester, reviewer* — coordinated by the main agent. Coding-heavy
roles run on the [Codex CLI](https://github.com/openai/codex) (`codex exec` in a
daemon thread); reasoning roles run on the main model. Live progress streams back
to the chat UI — Discord today, with a platform-agnostic core.

It installs entirely as a plugin — **no fork of hermes required**.
(Formerly `subagent_coder`; renamed to reflect that it is now a multi-role
collaboration plugin, not a single coder.)

## Roles

`delegate_task_background` takes a `role` parameter. Each role has its own
instructions, toolset, and model provider:

| role | runs on | tools | purpose |
|---|---|---|---|
| `coder` *(default)* | Codex CLI | terminal, file | implement / change code |
| `planner` | main model | file (no terminal) | investigate, then write an implementation plan |
| `tester` | Codex CLI | terminal, file | write & run tests, report pass/fail |
| `reviewer` | main model | file, **pii** | critique + scan for leaked secrets/PII before release |

The main agent chooses roles per its persona: `planner` for unclear/large work,
`coder` for clear implementation, `tester` to verify, and `reviewer` to **gate
anything before it ships**. The reviewer's `scan_pii` tool reuses hermes's own
redaction engine to flag emails, keys, tokens, IPs, and real home paths across
the workspace.

## What it adds

- **`delegate_task_background` tool** (with a `role` parameter) — hands a task to
  a detached sub-agent run instead of blocking the main turn.
- **Orchestration tools** — `coder_status` (running/finished runs + remaining
  capacity) and `cancel_coder`. Scope is limited to agent-spawned runs; `/code`
  slash runs are excluded.
- **Completion wake** — when a sub-agent finishes, a synthetic internal message
  is injected into the main session so the agent wakes and can schedule dependent
  follow-ups. Mirrors hermes's stock completion notification — **no polling**.
- **`scan_pii` tool** — the reviewer's dedicated pre-release secret/PII scan.
- **`codex-exec` model provider** — wraps the Codex CLI in an OpenAI
  chat-completion shape.
- **Discord overlay** — `/code <task>` deterministic spawn; a dedicated thread
  per run with live, debounced progress (including non-codex roles such as
  planner/reviewer); follow-up messages resume the same session; `stop`/`cancel`
  terminates.

## Benchmark: solo vs. collaboration

How much does the role fleet actually buy you over a single coder? We ran the
same task two ways — **solo** (one coder does everything) vs **collaboration**
(planner → coder → tester → reviewer) — on the **same model** so the only
variable is the workflow. We measured time, tokens, human interventions, and
output quality (tests, hidden edge-cases, and whether a secret planted in fixture
data leaked into the deliverable).

| task | solo | collaboration | verdict |
|---|---|---|---|
| **Easy** — 4 small utilities | 16 tests pass, 0 leaks · ~107K tokens · 9.5 min | same quality · ~803K tokens · 26 min | collaboration = **pure overhead** (~7.5× tokens for the same result) |
| **Hard + sensitive** — 6 validators with tricky edge cases (leap years, IPv4 octets) + a realistic API key buried in fixtures | **incomplete** (1 validator missing) and **leaked the API key** into a test · ~184K tokens · 4.4 min | **complete (6/6), the key stripped by the reviewer**, 85 tests · ~453K tokens · 30 min | collaboration **wins** despite ~2.5× cost |

**Takeaway:** the collaboration premium (≈2.5–7.5× tokens) is *not* always
justified. On easy / low-sensitivity work a single coder is faster and cheaper
for the same result. On hard or sensitive work the specialized **tester**
(catches omissions) and **reviewer** (catches leaked secrets/PII) are what turn a
fast-but-broken draft into a release-ready result.

> One run per cell — directional, not statistically rigorous. Cost is
> token-based (Codex usage is subscription-billed).

## Requirements

- Hermes (`hermes-agent`) installed.
- The `codex` CLI on `PATH`.
- Codex auth at `~/.codex/auth.json` (run `codex login`).
- For the Discord features: a Discord bot token configured in hermes
  (`DISCORD_BOT_TOKEN`) and `discord.py` (a hermes messaging dependency).

## Install

```bash
hermes plugins install bykim0119/hermes-agent-company
hermes plugins enable agent_company
```

This clones the repo into `~/.hermes/plugins/agent_company/`. hermes discovers it
on next start and runs its `register(ctx)`.

To update or remove:

```bash
hermes plugins update agent_company
hermes plugins remove agent_company
```

## Usage

- **Slash command:** `/code add a /health endpoint and a test for it`
- **Natural language:** ask hermes to do a coding task; it picks
  `delegate_task_background` and opens a thread.
- **Roles:** ask for the right shape of help — e.g. "plan this first" (planner),
  "now implement it" (coder), "verify with tests" (tester), "check it for leaks
  before we publish" (reviewer).
- **Follow up:** reply in the run's thread — it resumes the same session.
- **Cancel:** send `stop` (or `cancel`) in the thread.

## Orchestrating multiple sub-agents

For a multi-task job, the main agent can run several sub-agents and chain
dependent work:

- Fire **independent** tasks in parallel — call `delegate_task_background`
  multiple times in one turn.
- A **dependent** task is delegated only after the completion wake(s) it depends
  on arrive: the agent ends its turn, gets woken when each run finishes, then
  delegates the next step.
- `coder_status` reports active runs and remaining capacity (capacity is
  `HERMES_CODER_MAX_CONCURRENT`); `cancel_coder` stops a run going the wrong way.

> **Note on capacity:** completed runs currently count against the concurrency
> cap until the gateway restarts, so a long collaboration that spawns many roles
> can exhaust slots mid-run. Until that's fixed, raise
> `delegation.coder.max_concurrent` (config changes apply without a restart).

### The mechanism vs. the agent's role

The plugin provides the **mechanism** (spawn / observe / cancel / wake) and the
**role catalog**. It does **not** decide *what* to parallelize or *which role* to
use for a given step — that is governed by the agent's **persona / system
prompt**, which lives in your hermes config (`~/.hermes/SOUL.md` or your
equivalent), **not in this plugin**.

In practice the agent only behaves as a disciplined orchestrator if its persona
says so. Add an orchestrator block to the persona, e.g.:

```text
When given coding work, you are the project orchestrator:
- Do NOT write code yourself. Delegate via delegate_task_background, choosing the
  role that fits: planner for unclear/large work, coder for clear implementation,
  tester to verify, reviewer to check for leaked secrets/PII before publishing.
- Split the request into independent vs dependent tasks. Fire independent tasks
  in parallel (several calls in one turn); delegate a dependent task only after
  the completion notification(s) it depends on arrive.
- After delegating, END your turn and wait — completions arrive automatically.
  Don't poll coder_status in a loop; call it only when the user asks for progress
  or to check capacity before a new delegation.
- When runs finish, collect and verify results, then report back. Never publish
  or submit before a reviewer has checked it.
```

> The persona file is part of your hermes setup, not this repo. Updating the
> plugin (`hermes plugins update`) does not change it — keep your persona under
> your own backup.

## Configuration

Tunables resolve in priority order **env var → `delegation.coder.<key>` in
hermes `config.yaml` → default**:

| Setting | Env var | Default |
|---|---|---|
| Idle session timeout (s) | `HERMES_CODER_IDLE_TIMEOUT_S` | `7200` |
| Max concurrent runs | `HERMES_CODER_MAX_CONCURRENT` | `3` |
| Progress debounce (ms) | `HERMES_CODER_DEBOUNCE_MS` | `250` |
| Allow Discord DMs | `DISCORD_ALLOW_DMS` | `true` |

## Development

The package is a **flat layout** — `__init__.py` and its sibling modules live at
the repo root, because `hermes plugins install` loads the plugin from the repo
root of the clone. The tests load that flat package as `agent_company` (see
`tests/conftest.py`). Files prefixed `codex_*` wrap the Codex CLI; the role/
orchestration modules are `roles.py`, `orchestration.py`, `event_bus.py`,
`sessions.py`, `config.py`, `progress_formatter.py`.

The tests import and monkey-patch stock hermes modules (`run_agent`, `tools.*`,
`gateway.*`), so hermes must be importable:

```bash
pip install -e ".[dev]"   # or otherwise have hermes-agent importable
pytest
```

## License

See the upstream hermes-agent project.
