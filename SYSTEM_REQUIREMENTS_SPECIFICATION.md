# Agent Robber — System Requirements Specification

Local-first coding agent. One Python daemon owns agent logic; the Textual TUI, Vite GUI, and optional Tauri shell are clients. Interfaces never call models, tools, or MCP directly.

| Item | Value |
| --- | --- |
| Daemon | FastAPI `127.0.0.1:8787` (`server.py`) |
| GUI | Vite `127.0.0.1:5173` (`gui/`) |
| TUI | `python -m robber.tui` |
| Persistence | SQLite `.robber/accounts.db` |
| SSOT index | `.robber/ssot.json` (hash-json) |
| MCP catalog | `.robber/mcp.json` (no secrets) |
| Working context | Unbounded operator budget (K / M / B / T shorthand). Default 200,000 tokens. |

---

## 1. Backend architecture

```
Clients (TUI / GUI / Tauri)
        │  WebSocket / REST
        ▼
FastAPI app  (single Hub, no dual-listener)
        ├── AgentLoopManager  ─ bind_runtime(run_turn, dispatch)
        ├── WorkerPool        ─ Playwright stealth workers (lazy start)
        ├── SsotWatcher       ─ fingerprint + debounced reindex
        ├── McpHost           ─ builtins + stdio JSON-RPC
        ├── CustomModelManager + FailoverPoolRegistry
        ├── Database (aiosqlite, WAL)
        └── TerminalSupervisor (jailed subprocesses)
```

### Boot sequence (`@app.on_event("startup")`)

1. `agent_loop.bind_runtime(run_turn, dispatch, turn_lock, snapshot)`
2. SQLite `connect()` + `init_schema()` (creates missing tables, including `conversation_history`)
3. Restore SSOT context window from DB or prefs
4. Bootstrap custom models, proxies, accounts
5. Background tasks: model discovery, SSOT reindex + watcher, MCP host start

### Shutdown (`@app.on_event("shutdown")`)

Each step is isolated with try/except:

1. Stop `SsotWatcher` (cancel watch loop)
2. `WorkerPool.stop_all()` (cancel running profile workers)
3. `McpHost.stop()` (terminate MCP stdio processes)
4. `database.close()` (release SQLite)

### CORS

`allow_credentials=True` with an explicit origin list (no `"*"`):

- `http://127.0.0.1:5173` / `http://localhost:5173` (Vite)
- `http://127.0.0.1:4173` / `http://localhost:4173` (preview)
- `http://127.0.0.1:1420` / `http://localhost:1420` (Tauri dev)
- `http://127.0.0.1:8787` / `http://localhost:8787`
- `https://tauri.localhost` / `http://tauri.localhost`

Extra origins: `ROBBER_CORS_ORIGINS` comma list. Literal `*` is ignored.

---

## 2. REST API

Base: `http://127.0.0.1:8787`

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Daemon identity, WS paths, SSOT snapshot, slash catalog |
| GET | `/api/system/health` | Agent-loop health tree |
| POST | `/api/system/route-mode` | Combo route tier (`ECO` / `BALANCED` / `BEST`) |
| GET | `/api/history` | Session turns (`session_id`, `limit`) |
| DELETE | `/api/history` | Clear session turns |
| POST | `/api/context/paste` | Ingest large paste buffer |
| GET/POST | `/api/ssot/window` | Packed-context window |
| GET | `/api/commands` | Slash-command catalog |
| POST | `/api/commands/run` | Run a slash command |
| GET/POST | `/api/models` | List / create orchestration models |
| POST | `/api/models/add` | Add model (legacy body) |
| PUT/DELETE | `/api/models/{model_id}` | Patch / delete |
| POST | `/api/models/{model_id}/test` | Latency probe |
| POST | `/api/models/status` | Refresh live/fallback flags |
| POST | `/api/models/batch-mode` | Eco / normal / heavy batch |
| GET | `/api/models/discovered` | Discovery cache + failover pool |
| POST | `/api/models/discover` | Probe OpenAI-compatible endpoints |
| POST | `/api/models/discovery/override` | Enable/disable a discovered id |
| GET | `/api/models/ollama/scan` | Local Ollama tags |
| GET/POST | `/api/combos` | Combo pools by tier |
| GET | `/api/presets` | Provider presets |
| GET | `/api/accounts` | Profiles + worker status |
| POST | `/api/accounts/add` | Register profile |
| DELETE | `/api/accounts/{profile_id}` | Remove profile + browser session |
| GET | `/api/mcp` | MCP catalog snapshot |
| POST | `/api/mcp/enable` `/disable` `/add` `/call` | MCP lifecycle |
| DELETE | `/api/mcp/{server_id}` | Remove non-builtin server |
| GET/POST/PUT/DELETE | `/api/memo/*` | MeMO chunk store |
| GET/POST | `/api/fb_shark/*` | FB Shark worker control |

History REST:

```json
GET /api/history?session_id=current_session&limit=200
{ "session_id": "current_session", "turns": [
    { "id": 1, "session_id": "current_session", "role": "user",
      "content": "...", "tokens": 12, "timestamp": "2026-09-03T18:00:00+00:00" }
]}
```

---

## 3. WebSocket protocol

| Socket | Path |
| --- | --- |
| Workbench / GUI | `/ws` |
| Dashboard alias | `/ws/dashboard` |

On connect the daemon sends, in order: `snapshot()` (state), `loop_state`, `history`, then broadcasts `client_joined`.

Heartbeat: `{"type":"ping"}` every 30s. Clients ignore pings.

### Client → daemon (selected)

| `type` | Alias | Effect |
| --- | --- | --- |
| `prompt` | `EXECUTE_PROMPT`, `USER_PROMPT` | Run an agent turn |
| `get_history` | `GET_HISTORY`, `history` | Push `history` frame for `session_id` |
| `clear_history` | `CLEAR_HISTORY` | Wipe SQLite turns, push empty `history` |
| `set_mode` | `SET_MODE` | Turn mode or dashboard mode |
| `set_model` | | Active catalog label |
| `slash_command` | | `/help`, `/undo`, … |
| `ingest_paste` | `INGEST_PASTE` | Large paste offload |
| `KILL_TASK` | `purge` | Cancel turn / kill jobs |
| `mcp_call` | | Invoke MCP tool |
| `heal_approve` / `heal_reject` | | Manual checkpoint |

### Daemon → client (selected)

| `type` | Meaning |
| --- | --- |
| `state` | Snapshot: mode, model, busy, SSOT, mutations, keys |
| `message` | Streamed role/content (`append` for tokens) |
| `history` | `{session_id, turns[]}` for reconnect hydration |
| `busy` | thinking / memory / tool phase |
| `mutations` | File mutation cards |
| `memory_hits` | SSOT retrieval hits |
| `loop_state` | Control-mode tree, workers, healer |

Reconnect: GUI seeds chat only when the local message list is empty (fresh load). TUI replaces the seed pane once from the first `history` frame (last 50 turns).

---

## 4. SQLite schema

File: `{workspace}/.robber/accounts.db`  
Pragmas: `foreign_keys=ON`, `journal_mode=WAL`  
API keys in `models` / `custom_models` are Fernet-encrypted (`ROBBER_ACCOUNT_KEY` or `.robber/account.key`).

| Table | Role |
| --- | --- |
| `accounts` | Automation profiles |
| `session_cookies` | Playwright cookies per profile |
| `execution_logs` | Worker action log |
| `ssot_context` | Packed-window tokens (singleton `id=1`) |
| `custom_models` | Legacy custom endpoints |
| `models` | Orchestration catalog (protocol, base_url, fallback) |
| `combo_models` | ECO / BALANCED / BEST pools |
| `conversation_history` | Chat turns |

### `conversation_history`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER PK | Auto-increment |
| `session_id` | TEXT | Default `current_session` |
| `role` | TEXT | `user`, `assistant`, `error`, `system` |
| `content` | TEXT | Turn body |
| `tokens` | INTEGER | Estimate `(len+3)//4` |
| `timestamp` | TEXT | UTC ISO-8601 |

Index: `(session_id, timestamp, id)`.

**Persist policy:** on each `run_turn`, write the user prompt immediately; write the assistant `last_output` in `finally`; write `error` if the turn raises. Intermediate tool/memory/canvas frames are not stored.

---

## 5. MCP tool contracts

Catalog: `.robber/mcp.json`. Builtins cannot be deleted. Stdio binaries are restricted to `python` / `node` and module `robber.mcp.echo_server`.

### `jailed-fs` (builtin)

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `list_dir` | `path?` | List directory via `resolve_inside(workspace)` |
| `read_file` | `path` | Read a text file |
| `search_files` | `query` (`q`), `path?` | Filename + body substring search (cap 20 hits, skip secrets / skipped dirs) |

### `ssot-memory` (builtin)

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `search` | `query` (`q`) | Hash-json index search, formatted hits |

### `git-readonly` (builtin)

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `status` | — | `git status --short --branch` |
| `diff` | — | `git diff --stat` |
| `log` | — | `git log -8 --oneline` |

No push, reset, or commit.

### `echo-stdio` (stdio probe, disabled by default)

Command: `python -m robber.mcp.echo_server`  
Transport: LSP-style `Content-Length` JSON-RPC. Used to verify stdio lifecycle; stopped on daemon shutdown.

REST/WS call shape: `{ "server": "jailed-fs", "tool": "read_file", "arguments": { "path": "README.md" } }`.

---

## 6. Router failover

Combo routing (`CustomModelManager.engines_for_prompt`) picks a live engine list from the active tier combo (reasoning / coding / general), skipping quarantined ids, then fills from `FailoverPoolRegistry`.

`stream_with_failover` (up to 8 hops):

1. Local retry: `stream_model` retries the same engine 3 times with exponential backoff (0.4s, 0.8s, 1.6s) on transient timeouts / 429 / 5xx before giving up.
2. Skip members already in `FailoverPoolRegistry` quarantine (default 5 minutes).
3. `pick_healthy_engine`: live key/Ollama, else one-hop `fallback_label`.
4. Stream tokens. On `is_failover_error` after local retries, quarantine the model and hop to `pool.next_after`.
5. Record TTFT latency on success (`note_latency`) so later ranking prefers faster members.

`AgentLoopManager._run_agent_turn` also wraps `run_turn` in the same 3-attempt backoff for unexpected transient raises.

MCP stdio servers are pinged every 30s (`McpHost` watchdog) and auto-restarted if the subprocess dies or `ping` fails. Builtins are not pinged.

`is_failover_error` lives in `robber/orchestrator.py`. Discovery results hydrate the pool; `/api/models/discover` refreshes it.

If no live engine remains, the turn uses the local fallback reply. The daemon stays up.

---

## 7. Agent turn (happy path)

1. Persist user prompt.
2. Classify intent; pack SSOT context (skipped for inquiries).
3. Select and run a workspace tool (writes blocked in plan/review; shell blocked in manual until checkpoint).
4. Attach MCP prompt catalog + tool output.
5. Stream via combo/hybrid/distributed-brain assignment with failover.
6. Apply file mutations (manual mode waits for accept).
7. Persist assistant output; broadcast snapshot.

Control modes: `manual` | `autonomous`. Turn modes: `plan` | `act` | `review`.

---

## 8. Runtime invariants

- Bind `127.0.0.1:8787` only. Recycle that port after daemon code changes; do not kill TUI/Vite.
- `WorkerPool` is constructed at import and injected into `AgentLoopManager`. Playwright launches only when a worker starts.
- `SsotWatcher` is bound at import and started in `_boot_ssot`. `ssot_payload()` never assumes a live watcher or index.
- `python server.py` must fail if 8787 is already bound (single Hub).
- No API keys in committed JSON.
