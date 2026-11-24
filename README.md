# Agent Robber

**A local-first coding agent with a hybrid interface: one Python daemon does the work, and every interface attaches to it over WebSocket.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61DAFB.svg)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-6-646CFF.svg)](https://vitejs.dev/)
[![Tauri 2](https://img.shields.io/badge/Tauri-2-24C8DB.svg)](https://tauri.app/)
[![MCP](https://img.shields.io/badge/MCP-local%20host-6E56CF.svg)](https://modelcontextprotocol.io/)

Agent Robber runs entirely on your machine. The daemon owns the agent loop, the
workspace jail, the model router, the memory index, and the MCP host. Interfaces
are deliberately thin: they render state and send frames. That means a terminal,
a browser dashboard, and a desktop window can all drive the same brain without
duplicating logic — or leaking it into a webview.

**The rule:** *interfaces never contain agent logic.* The TUI is the agent
surface. The GUI is a Control Center for configuration, maintenance, and
monitoring. Tauri only opens that GUI and, if port `8787` is free, starts
uvicorn. It never calls models, tools, or MCP.

---

## Highlights

| Principle | How it is implemented |
| :--- | :--- |
| **Local-first privacy** | Everything — daemon, memory index, vault, MCP host — runs on `127.0.0.1`. No hosted backend, no account, no telemetry endpoint. |
| **Path-jailed workspace execution** | Every filesystem and command tool resolves paths through `robber/jail.py`; reads, writes, diffs, and shell commands are confined to the workspace root. |
| **Human-in-the-loop checkpoints** | The agent loop pauses on a ready patch and waits for `/approve` or `/reject` (also exposed as `POST /api/policy/approve` and `/api/policy/deny`). Autonomy is a mode, not the default assumption. |
| **Zero-telemetry local embeddings** | `robber/embed.py` builds 96-dimensional hash vectors with BLAKE2b on CPU. No model download, no API call, no GPU requirement — it fits an 8 GB machine. |
| **SSOT memory with a live watcher** | One hash-vector index at `.robber/ssot.json`, refreshed by a debounced jailed file watcher (`robber/ssot_watch.py`) and shared by every model and interface. |
| **Local jailed MCP host** | A marketplace and stdio JSON-RPC host (`robber/mcp/`) with built-in `jailed-fs`, `ssot-memory`, and `git-readonly` adapters. No unsigned package downloads; env keys are passed by name only. |
| **BYOK LiteLLM router** | `robber/router/` routes to Groq, Gemini, OpenAI, Anthropic, OpenRouter, Ollama, LM Studio, vLLM, or llama.cpp with per-provider keys, rotation, and failover. |
| **Encrypted credential vault** | AES-256 vault (`robber/vault.py`) for keys and account material; `/keys` reports presence only and never prints a secret. |
| **Desktop shell without filesystem access** | The Tauri 2 webview is locked to localhost by CSP and capabilities; it cannot shell out or read the disk. |

---

## Architecture

```
+-------------------------------------------------------------------------------+
|  DAEMON  -  server.py  -  FastAPI  :8787                                      |
|  turn pipeline | LiteLLM router | SSOT index + watcher | MCP host             |
+-------------------------------------------------------------------------------+
            ^                           ^                           ^
            |  WS /ws                   |  WS /ws                   |  WS /ws
+-----------------------+   +-----------------------+   +-----------------------+
| Textual TUI           |   | Ink CLI               |   | Control Center        |
| robber/tui            |   | my-agent/             |   | React + Vite  :5173   |
| python -m robber.tui  |   | npm run tui:ink       |   | Tauri 2 shell         |
+-----------------------+   +-----------------------+   +-----------------------+
```

The daemon is the only process that touches models, files, or MCP. Clients hold
no agent state; they reconnect on their own when the daemon restarts.

### Interfaces

| Interface | Location | Run | Role |
| :--- | :--- | :--- | :--- |
| **Workbench TUI** | `robber/tui/` | `python -m robber.tui` | Native Textual agent surface with the reasoning canvas |
| **Ink CLI** | `my-agent/` | `npm run tui:ink` | Ink + React terminal client |
| **Control Center GUI** | `gui/` | `npm run gui` | React + Vite "Obsidian Glass" dashboard on `127.0.0.1:5173` |
| **Desktop shell** | `gui/src-tauri/` | `npm run desktop` | Tauri 2 window around the same GUI (requires Rust) |

---

## Quickstart

> **Run every command from the repository root** — the folder that contains
> `server.py`, `robber/`, and `package.json`. In the original design bundle this
> folder was named `CLI Version/`; it is now the repository root, so do **not**
> `cd` into a subfolder unless a command below says to.

**Requirements:** Python 3.11+, Node.js 22+ (optional TUI/GUI), Rust (optional,
desktop shell only).

```bash
# 1. Python dependencies (repo root)
pip install -r requirements.txt

# 2. Optional: bring your own key (the daemon runs without one)
cp .env.example .env          # Windows: copy .env.example .env
# then uncomment GROQ_API_KEY= or GEMINI_API_KEY=

# 3. Start the daemon (repo root) -> http://127.0.0.1:8787
npm run daemon                # or: python server.py
```

In a second terminal, from the same repo root, attach an interface:

```bash
# Native Textual workbench
python -m robber.tui          # or: npm run tui

# Ink CLI
npm run tui:ink

# Control Center GUI -> http://127.0.0.1:5173
npm --prefix gui install
npm run gui

# Desktop window (Rust toolchain required)
npm run desktop
```

Without a key the daemon still answers with a local fallback, so the full turn
pipeline and reasoning canvas are testable offline. Both clients speak the same
WebSocket protocol; the header (TUI) and header pill (GUI) show daemon link
state, and clients stay usable offline while reconnecting.

If `:8787` is already serving the daemon, Tauri reuses it. Otherwise it starts
`python -m uvicorn server:app --host 127.0.0.1 --port 8787` from the repo root
and stops that child when the window closes.

---

## Scripts

| Command | What it does |
| :--- | :--- |
| `npm run daemon` | Start the FastAPI daemon on `127.0.0.1:8787` |
| `npm run tui` | Native Textual workbench (`python -m robber.tui`) |
| `npm run tui:ink` | Ink CLI in watch mode |
| `npm run gui` | Glass GUI on `127.0.0.1:5173` |
| `npm run gui:build` | Production-build the GUI |
| `npm run desktop` | Tauri window around the GUI (requires Rust) |
| `npm run desktop:build` | NSIS installer via Tauri (requires Rust) |
| `npm run build` | Compile the Ink CLI to `my-agent/dist` |
| `npm run typecheck` | Type-check the TUI packages |
| `npm run smoke` | Offline TUI test (no daemon needed) |
| `npm run verify` | Integration test — **requires a running daemon** |

---

## Workbench TUI shortcuts

| Key | Action |
| :--- | :--- |
| `Ctrl+C` / `F3` | `KILL_TASK` |
| `Ctrl+B` / `F1` | Toggle Beast Mode |
| `F2` | Toggle prompt history |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo step |
| `Ctrl+A` / `Ctrl+R` | Accept All / Revert All |
| `Tab` / `Ctrl+S` | Expand or collapse the right sidebar |
| `Ctrl+P` | Plan Mode |
| `Ctrl+L` | Clear the stream |
| `Ctrl+Q` | Quit |

`/exit` also quits. Other slash commands are forwarded to the daemon.

---

## TUI commands

| Command | Effect |
| :--- | :--- |
| `/help` | List commands |
| `/approve [id]` | Accept the pending patch/checkpoint |
| `/reject [id]` | Reject it and keep the workspace unchanged |
| `/canvas` | Toggle the dual-pane reasoning canvas |
| `/keys` | Show which BYOK providers are configured (never prints secrets) |
| `/purge [pid]` | Kill agent-spawned subprocesses |
| `/status` | Show daemon URL, link, mode, model, engine, live flag |
| `/memory [q]` | Query the live SSOT index (`reindex`, `watch`, `window` subcommands) |
| `/window [n]` | Show or set the SSOT unified context window (`/window 1t`, `/window 8m`); default 1 trillion tokens |
| `/mcp` | List the local MCP catalog (`enable`, `disable`, `call`) |
| `/theme [dark\|light]` | Switch appearance — synced with the GUI Control Center |
| `/clear` | Clear the transcript (also `Ctrl+L`) |
| `/exit` | Quit |

Anything else is sent to the daemon, which runs the turn and streams phases
back. Reasoning goes to the canvas pane; the answer goes to the conversation.

---

## WebSocket protocol

Both endpoints (`/ws` for clients, `/ws/dashboard` for the Control Center) carry
JSON frames.

**Client to daemon**

| Frame | Payload |
| :--- | :--- |
| `prompt` | `{ "type": "prompt", "text": "..." }` |
| `set_mode` | `{ "type": "set_mode", "mode": "plan\|act\|review" }` |
| `set_model` | `{ "type": "set_model", "model": "..." }` |
| `keys_status` | `{ "type": "keys_status" }` |
| `memory_query` | `{ "type": "memory_query", "text"?: "..." }` |
| `set_context_window` | `{ "type": "set_context_window", "window": "1t" }` |
| `purge` | `{ "type": "purge", "pid"?: number }` |
| `add_model` | `{ "type": "add_model", "label": "...", "provider": "...", "litellm_model": "...", "api_key"?: "..." }` |
| `set_key` | `{ "type": "set_key", "provider": "...", "secret": "..." }` — presence-only after save |
| `set_consensus` | `{ "type": "set_consensus", "models": ["Gemini Ultra", ...] }` |
| `memory_inject` | `{ "type": "memory_inject", "key": "...", "value": "...", "category"?: "...", "tags"?: [] }` |
| `memory_purge` | `{ "type": "memory_purge" }` |
| `set_theme` | `{ "type": "set_theme", "theme": "dark\|light" }` |
| `mcp_list` | `{ "type": "mcp_list" }` |
| `mcp_enable` / `mcp_disable` / `mcp_remove` | `{ "type": "mcp_enable", "id": "jailed-fs" }` |
| `mcp_add` | `{ "type": "mcp_add", "id": "...", "command": "python", "args": ["tools/my_mcp.py"], "env_keys": ["GEMINI_API_KEY"] }` |
| `mcp_call` | `{ "type": "mcp_call", "server": "jailed-fs", "tool": "list_dir", "arguments": {} }` |

**Daemon to client**

| Frame | Meaning |
| :--- | :--- |
| `state` | Mode, model, engine, live flag, theme, workers, key presence, catalog, consensus, notes, busy, client count, SSOT stats, context window, MCP snapshot |
| `mcp` / `mcp_result` | Marketplace snapshot after `/mcp`; tool result (no secret values) |
| `memory_hits` | Structured SSOT hits + notes after `memory_query` |
| `message` | Transcript entry. `role` includes `reasoning` (canvas). `append: true` concatenates onto the last message of that role (token streaming) |
| `busy` | Current phase (`thinking`, `memory`, `tool`) plus a label |
| `event` | Connection lifecycle notices |
| `error` | Rejected or malformed request |

---

## Security & privacy model

- **Workspace jail.** All file and command tools resolve through the jail;
  escapes are rejected, not sanitized. Python patches are compile-checked before
  apply.
- **Approval gates.** A ready patch stops the loop until a human accepts it.
  `Ctrl+C` / `F3` kills a running task; `/purge` cleans up spawned children.
- **Secrets never leave the daemon.** Keys live in `.env` or the AES-256 vault.
  `/keys` and the GUI report configured/not-configured only. `.env` and
  `.robber/` are git-ignored.
- **MCP is local-only.** No registry fetch, no unsigned installs. `mcp_add`
  requires a script already inside the jail, and only passes environment
  variable *names*.
- **Login walls are never bypassed.** Captcha, 2FA, and login challenges pause
  the run and raise an alert for a human; the agent does not solve them.
- **No telemetry.** The only outbound traffic is to the model provider you
  configure (or nothing at all in fallback mode).

---

## Phase status

| Phase | Status | What shipped |
| :--- | :--- | :--- |
| 0 | Done | One daemon, TUI attached over WebSocket |
| 1 | Done | Dual-pane reasoning canvas, BYOK vault, LiteLLM router with canned fallback |
| 2 | Done | Workspace path jail, list/read/git/allowlisted commands, terminal ring buffer, timeout watchdog, `/purge` |
| 3 | Done | SSOT index (tree-sitter chunks), local hash embeddings, unified diffs, compile-checked Python patches |
| 4 | Done | Obsidian Glass Control Center: Overview, Configuration, Mem0 Store, CLI Console, Logs |
| 5 | Done | Jailed patch apply, self-healing from stderr, vision inspector, challenge pause/alert |
| 6 | Done | `AgentLoopManager` with autonomous/manual control, heal checkpoints (`/approve`, `/reject`), `GET /api/system/health` |
| 7 | Done | Local MCP host and marketplace: built-ins, jailed stdio client, `.robber/mcp.json` catalog |
| 8 | Done | Tauri 2 desktop shell around the Control Center; CSP and capabilities stay on localhost |
| 9 | Done | Live SSOT file watcher: debounced fingerprinting and reindex without transcript spam |
| 10 | Done | Adjustable SSOT unified context window (default 1T tokens) persisted to SQLite and `.robber/prefs.json` |

---

## Layout

```
server.py           FastAPI daemon: hub, turn pipeline, router, MCP host
robber/             Vault, canvas, router, jail, tools, terminal, SSOT, patch, MCP
  router/           LiteLLM routing, key rotation, failover, provider proxy
  mcp/              Local marketplace catalog, builtin adapters, stdio JSON-RPC
  ssot.py           Hash-vector workspace index
  ssot_watch.py     Debounced jailed file watcher
  ssot_context.py   Adjustable unified context window + packer
  embed.py          BLAKE2b 96-dim local embeddings (no model download)
  tui/              Textual workbench client
.env.example        BYOK key template (copy to .env)
requirements.txt    Daemon dependencies
my-agent/           Ink CLI (Ink + React + TypeScript, ESM)
gui/                Control Center dashboard (React + Vite)
  src-tauri/        Tauri 2 window + optional uvicorn spawn (no agent logic)
ui/                 Experimental TypeScript TUI for the Go gateway
gateway/            Go routing gateway and bridge
indexer/            Rust indexing helper
tests/              Pytest suite and end-to-end checks
benchmarks/         Local benchmarks
```

---

## Contributing

Contributions are welcome — especially around the jail, the SSOT indexer, MCP
adapters, and router failover.

1. Fork the repository and branch from `main`: `feat/short-description`.
2. Run the checks from the repo root: `npm run typecheck`, `npm run smoke`, and
   `pytest -q`.
3. `npm run verify` needs a daemon on `127.0.0.1:8787`; start one with
   `npm run daemon` first.
4. Keep interfaces logic-free. Agent behavior belongs in `server.py` or
   `robber/`, never in `gui/`, `my-agent/`, or `ui/`.
5. Never commit `.env`, `.robber/`, browser profiles, databases, or keys.
6. Report security issues privately via a GitHub security advisory, not a
   public issue.
7. By contributing you agree your work is licensed under the MIT License.

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 gfxmaster7020410-cloud.
