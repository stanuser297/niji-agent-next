# Niji Agent

An open-source, provider-agnostic coding agent for your terminal, with a private local web UI.
Bring your own API key (OpenAI-compatible providers, Groq, NVIDIA, xAI, Ollama and more).

- **Terminal agent**: `niji` for chat, `niji "fix the failing test"` for one-shot tasks.
- **Local UI**: `niji ui --open` serves a multi-screen app (Home, Chat, Files, Automations, Run history, Event log, Settings) on `127.0.0.1` only, behind a random per-run token. Add providers, enter API keys and switch models from Settings.
- **Approvals**: tool actions ask for your approval by default. The decision is made by the host program, not by model-generated code.
- **Typed event log**: every tool call, approval and run control is appended to `~/.niji/events/<session>.jsonl` (schema-versioned, secrets masked, mode 0600). Replay with `niji events <session-id>` or open the Event log screen.
- **Worktree isolation**: `niji worktree new <name>` makes a separate git worktree and `niji/<name>` branch so the agent never touches your main checkout. `niji worktree list`, `niji worktree rm <name>`.
- **Control**: pause, resume, cancel, undo last file change, diffs in Files & results, plan approval, memory, subagents, MCP connectors, scheduler.

## Install

Needs Python 3.10+ and git. One command (Linux, macOS, WSL, Termux):

```sh
curl -fsSL https://raw.githubusercontent.com/stanuser297/niji-agent-next/main/install.sh | sh
```

Termux: run `pkg install -y curl` first. Windows: use WSL, then the command above.

Or with pip:

```sh
pip install "git+https://github.com/stanuser297/niji-agent-next.git"
```

First launch runs a setup wizard (provider, API key, model). Keys are saved only on your device in `~/.niji/config.json` (private permissions). Nothing is sent anywhere except to the provider you choose.

## Run

```sh
niji                 # terminal chat
niji ui --open       # local UI at http://127.0.0.1:<port>/?token=... (loopback only)
niji doctor          # check setup
niji events          # list sessions with event logs
niji worktree new try-1 && cd ../.<repo>-niji-worktrees/try-1 && niji
```

The UI refuses non-loopback hosts, checks Host and Origin headers, and requires its token on every API call.

## Develop

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test,cloud,documents]"
pytest -q
```

Some terminal-composer (pty) tests depend on your terminal environment and can fail in minimal containers.

## Status and limits

This is a fork of an earlier private project, so the history was reset. The hosted "cloud runtime" modules (`cloud_*`) are inherited and not needed for local use. Windows without WSL is untested. See [docs/CHANGELOG.md](docs/CHANGELOG.md) for inherited release notes.

MIT licensed.
