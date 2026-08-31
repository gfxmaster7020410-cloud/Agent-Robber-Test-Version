# Agent Robber

A local-first coding agent with a hybrid interface: a Python daemon does the
work, and interfaces attach to it over WebSocket. The CLI TUI, a glass GUI
dashboard, and a Tauri desktop window all attach to the same brain.

```
                +---------------------------+
                |  Daemon (FastAPI, :8787)  |   <- the only "brain"
                +---------------------------+
                   ^           ^           ^
        WebSocket  |           |           |
                   |           |           |
        +----------+  +--------+  +-------+
        | CLI TUI  |  | GUI    |  | Tauri |
        | my-agent |  | :5173  |  | shell |
        +----------+  +--------+  +-------+
```

The important rule: **interfaces never contain agent logic.** The TUI is the
agent surface. The GUI (browser or desktop window) is a Control Center for
configuration, maintenance, and monitoring — including adding models that share
the same SSOT memory. Tauri only opens that GUI and, if port 8787 is free,
starts uvicorn. It does not call models, tools, or MCP.

## Requirements

- Python 3.11+
- Node.js 22+ (the TUI uses the built-in global `WebSocket`)
- Rust (optional) — only for `npm run desktop` / `npm run desktop:build`

## Quickstart

Install dependencies:

```bash
pip install -r requirements.txt
npm --prefix my-agent install
```

Optional — add your own keys so the daemon can call a real model:

```bash
copy .env.example .env     # Windows
# then uncomment GROQ_API_KEY= or GEMINI_API_KEY=
```

Without keys the daemon still runs, using a local fallback reply. The reasoning
canvas still fills in either case.

Run the daemon in one terminal:

```bash
npm run daemon          # or: python server.py
```

Run the TUI in another:

```bash
npm run tui
```

Or the glass GUI dashboard:

```bash
npm --prefix gui install
npm run gui
```

Open `http://127.0.0.1:5173`. Both clients speak the same WebSocket protocol. The
header and footer (TUI) / header pill (GUI) show the daemon link state. If the
daemon is down the client stays usable, reports offline, and reconnects on its
own once it is back up.

Desktop window (needs the Rust toolchain):

```bash
npm --prefix gui install
npm run desktop
```

If `:8787` is already serving the daemon, Tauri reuses it. If not, it starts
`python -m uvicorn server:app --host 127.0.0.1 --port 8787` from the repo root
and stops that child when the window closes. The webview has no filesystem or
shell permissions.

## Scripts

| Command | What it does |
| --- | --- |
| `npm run daemon` | Start the FastAPI daemon on `127.0.0.1:8787` |
| `npm run tui` | Start the TUI in watch mode |
| `npm run gui` | Start the glass GUI on `127.0.0.1:5173` |
| `npm run gui:build` | Production-build the GUI |
| `npm run desktop` | Tauri window around the GUI (requires Rust) |
| `npm run desktop:build` | NSIS installer via Tauri (requires Rust) |
| `npm run build` | Compile the TUI to `my-agent/dist` |
| `npm run typecheck` | Type-check the TUI |
| `npm run smoke` | Offline TUI test (no daemon needed) |
| `npm run verify` | Integration test — **requires a running daemon** |

## TUI commands

| Command | Effect |
| --- | --- |
| `/help` | List commands |
| `/canvas` | Toggle the dual-pane reasoning canvas |
| `/keys` | Show which BYOK providers are configured (never prints secrets) |
| `/purge [pid]` | Kill agent-spawned subprocesses |
| `/status` | Show daemon URL, link, mode, model, engine, live flag |
| `/memory [q]` | Query the live SSOT index (`/memory reindex` rebuilds it, `/memory watch` shows the file watcher, `/memory window` shows or sets the context budget) |
| `/window [n]` | Show or set the SSOT unified context window (`/window 1t`, `/window 8m`). Default is 1 trillion tokens. Live — works while a turn is running. |
| `/mcp` | List the local MCP catalog (`/mcp enable\|disable <id>`, `/mcp call <server> <tool> [json]`) |
| `/theme [dark\|light]` | Switch appearance — synced with the GUI Control Center |
| `/clear` | Clear the transcript (also `Ctrl+L`) |
| `/exit` | Quit (also `Ctrl+C`) |

Anything else is sent to the daemon, which runs the turn and streams the phases
back. Reasoning goes to the canvas pane; the answer goes to the conversation.

## Protocol

Client to daemon:

| Frame | Payload |
| --- | --- |
| `prompt` | `{ "type": "prompt", "text": "..." }` |
| `set_mode` | `{ "type": "set_mode", "mode": "plan\|act\|review" }` |
| `set_model` | `{ "type": "set_model", "model": "..." }` |
| `keys_status` | `{ "type": "keys_status" }` |
| `memory_query` | `{ "type": "memory_query", "text"?: "..." }` — empty text searches a default query; `reindex` rebuilds; `window` / `window 1t` reads or sets the context budget |
| `set_context_window` | `{ "type": "set_context_window", "window": "1t" }` — also `SET_ACTIVE_WINDOW` `{ "limit": 1000000000000 }`. Persisted to SQLite + `.robber/prefs.json`. Live while idle or busy. |
| `purge` | `{ "type": "purge", "pid"?: number }` |
| `add_model` | `{ "type": "add_model", "label": "...", "provider": "...", "litellm_model": "...", "api_key"?: "..." }` — persists the route in `.robber/models.json`; optional key is written to `.env` and never echoed |
| `set_key` | `{ "type": "set_key", "provider": "...", "secret": "..." }` — presence-only after save |
| `set_consensus` | `{ "type": "set_consensus", "models": ["Gemini Ultra", ...] }` — Hybrid Multi-LLM tries these on one task |
| `memory_inject` | `{ "type": "memory_inject", "key": "...", "value": "...", "category"?: "...", "tags"?: [] }` |
| `memory_purge` | `{ "type": "memory_purge" }` — clears injected notes, not the file index |
| `set_theme` | `{ "type": "set_theme", "theme": "dark\\|light" }` — persisted; GUI and TUI both apply it |
| `mcp_list` | `{ "type": "mcp_list" }` — local marketplace snapshot |
| `mcp_enable` / `mcp_disable` / `mcp_remove` | `{ "type": "mcp_enable", "id": "jailed-fs" }` |
| `mcp_add` | `{ "type": "mcp_add", "id": "...", "command": "python", "args": ["tools/my_mcp.py"], "env_keys": ["GEMINI_API_KEY"] }` — script must already be inside the jail; env keys are names only |
| `mcp_call` | `{ "type": "mcp_call", "server": "jailed-fs", "tool": "list_dir", "arguments": {} }` |

Daemon to client:

| Frame | Meaning |
| --- | --- |
| `state` | Mode, model, engine, live flag, theme, workers, key presence, catalog, consensus, notes, busy, client count, SSOT stats, context window / packed tokens, MCP snapshot |
| `mcp` / `mcp_result` | Marketplace snapshot after `/mcp`; tool call result (no secret values) |
| `memory_hits` | Structured SSOT hits + notes after `memory_query` |
| `message` | Transcript entry. `role` includes `reasoning` (canvas). `append: true` concatenates onto the last message of that role (token streaming). |
| `busy` | Current phase (`thinking`, `memory`, `tool`) plus a label |
| `event` | Connection lifecycle notices |
| `error` | Rejected or malformed request |

## Status

Phase 0 is done: one daemon, TUI attached over WebSocket.

Phase 1 is in: dual-pane reasoning canvas, BYOK vault (env / `.env`), LiteLLM
router with a canned fallback when no key is configured.

Phase 2 is in: workspace path jail, real list/read/git/allowlisted commands,
terminal ring buffer, timeout watchdog, and `/purge` PID killer. Tools run in
the daemon; the TUI only displays results.

Phase 3 is in: SSOT memory indexes the workspace on daemon start (tree-sitter
chunks for Python, regex/windows for TS/JS/md, plus text configs that used to
be skipped: `.env.example`, `.gitignore`, `requirements.txt`, CSS). Secrets,
lockfiles, fonts, and `node_modules` stay out. Embeddings are local hash
vectors stored as JSON at `.robber/ssot.json` — no model download, fits an
8 GB machine. `/memory` and each turn search that index. Unified diffs wrapped
in a markdown `diff` fence apply inside the jail; Python patches are compile-checked.

Phase 4 is in: the GUI is an Obsidian Glass **Control Center** (`gui/` on
`127.0.0.1:5173`) matching the sample dashboard. Overview, Configuration, Mem0
Store, CLI Console, and Logs. Models are added from Configuration; Hybrid can
run several of them on the same TUI task against shared SSOT. Type uses Base
Neue Light / Regular / Medium only — no italics, no heavy weights.

Phase 5 is in: jailed patch apply, self-healing from stderr, and a vision
inspector that can classify checkpoints. Captcha / 2FA / login walls pause and
alert — they are never solved or bypassed.

Phase 6 is in: `AgentLoopManager` with autonomous / manual control, heal
checkpoints (`/approve` `/reject`), and `GET /api/system/health`.

Phase 7 is in: a **local MCP host and marketplace**. Built-in adapters
(`jailed-fs`, `ssot-memory`, `git-readonly`) plus a jailed stdio client.
Catalog lives at `.robber/mcp.json` (no secret values). There is no public
download of unsigned packages.

Phase 8 is in: a **Tauri desktop shell** (`gui/src-tauri/`) around the same
Control Center. No second agent. CSP and capabilities stay on localhost; the
webview cannot shell out or read the disk. `tauri dev` / `tauri build` need
Rust on PATH.

Phase 9 is in: a **live SSOT file watcher**. After boot, the daemon fingerprints
jailed source files (still skipping `.robber`, secrets, and lockfiles) and
reindexes when they change. `/memory watch` reports status. Transcript is not
spammed on every save.

Phase 10 is in: an **adjustable SSOT unified context window**. Default is
**1 trillion** tokens (logical budget, not a RAM buffer). It is stored in
SQLite (`ssot_context`) and `.robber/prefs.json`, can be changed live from the
GUI or `/window`, and is the pack cap for notes, `@file#Lstart-Lend` pins, and
ranked SSOT hits on every turn. High and low models share this memory layer.

## Layout

```
server.py           FastAPI daemon: hub, turn pipeline, router, MCP host
robber/             Vault, canvas, router, jail, tools, terminal, SSOT, patch, MCP
  mcp/              Local marketplace catalog, builtin adapters, stdio JSON-RPC
  ssot_watch.py     Debounced jailed file watcher for the hash-json index
  ssot_context.py   Adjustable unified context window (default 1T) + packer
.env.example        BYOK key template (copy to .env)
requirements.txt    Daemon dependencies
my-agent/           CLI TUI (Ink + React + TypeScript, ESM)
  source/           Layout, commands, useDaemon, components
  test/             smoke (offline) and verify (live daemon)
gui/                Control Center dashboard (React + Vite)
  src/assets/fonts/ Bundled Base Neue (+ Mono), Light/Regular/Medium only
  src/App.tsx       Header + sidebar + views — no agent logic
  src/views/        Overview, Configuration, MCP marketplace, Mem0, CLI Console, Logs
  src/hooks/        Same /ws protocol as the TUI
  src-tauri/        Tauri 2 window + optional uvicorn spawn (no agent logic)
```
