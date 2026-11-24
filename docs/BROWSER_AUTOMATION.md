# Browser Automation & Visual UI Debugging

## Overview

Agent Robber now includes a comprehensive **Autonomous Visual UI Debugging & Playwright Browser Automation Loop** that enables:

- 🌐 Headless browser automation with Chromium/Firefox
- 📸 Screenshot capture and DOM inspection
- 🔍 Real-time console error & network failure detection
- 🤖 Multimodal vision-based UI auditing
- 🔧 Self-healing frontend code repair loop
- 💾 Low-memory footprint (< 300 MB for browser instance)

## Architecture

### Module 1: Headless Playwright Browser Engine
**Location:** `robber/tools/browser_engine.py`

Low-footprint async browser controller with aggressive resource throttling:
- Launches Chromium/Firefox in strict low-spec mode
- Captures high-compression screenshots to `data/snapshots/browser/`
- Extracts full DOM hierarchy, computed CSS styles, and accessibility tree nodes

**Key Classes:**
- `PlaywrightBrowserEngine`: Main browser automation controller
- `BrowserSnapshot`: Dataclass containing page state, screenshots, DOM, and telemetry

### Module 2: Real-Time DOM, Console & Network Inspector
**Location:** `robber/diagnostics/browser_inspector.py`

Automated diagnostic listener for client-side errors:
- Intercepts `console.error`, `console.warn`, and uncaught JavaScript exceptions
- Captures HTTP 4xx/5xx, failed CORS, and broken API fetches
- Maps JavaScript runtime errors to source line numbers and DOM elements

**Key Classes:**
- `BrowserInspector`: Telemetry analyzer
- `BrowserInspectionReport`: Structured diagnostic payload with error localization

### Module 3: Visual-LLM Inspection & Frontend Auto-Fix Loop
**Location:** `robber/agents/vision_auditor.py`

Multimodal vision capabilities integrated into the worker swarm:
- Attaches browser screenshots and DOM snapshots to audit prompts
- Evaluates visual rendering issues (element overlaps, broken layouts, CSS misalignments)
- Correlates visual flaws with JS console tracebacks
- Constructs AST micro-diff fixes and verifies repairs

**Key Classes:**
- `VisionAuditorWorker`: Swarm worker for UI auditing and self-healing
- `VisionAuditResult`: Results dataclass with repair metadata

### Module 4: Orchestrator Integration & TUI Streaming
**Location:** `robber/commands/handlers.py`, `robber/agents/swarm_orchestrator.py`

- `/webtest` slash command for interactive browser testing
- Automatic dev server launch and port-binding detection
- Real-time TUI streaming of browser status, console errors, and snapshot paths
- Integration with `SwarmOrchestrator` worker pool

## Installation

1. Install Playwright and dependencies:
```bash
pip install playwright>=1.40
playwright install chromium
```

2. Verify installation:
```bash
python -c "from playwright.async_api import async_playwright; print('Playwright OK')"
```

## Usage

### Basic Web Testing

Test a running web application:
```bash
/webtest http://localhost:3000
```

### Launch Dev Server and Test

Start a dev server, wait for port binding, and test:
```bash
/webtest npm run dev
/webtest python -m http.server 8000
/webtest npx vite --port 5173
```

### Self-Healing UI Repair

When errors are detected, the `VisionAuditorWorker` automatically:
1. Analyzes console errors and network failures
2. Maps errors to source code locations
3. Synthesizes AST micro-diff fixes
4. Applies patches via Tree-Sitter structural editor
5. Reloads page and verifies fix
6. Rolls back if errors persist

## Configuration

**`robber/config/__init__.py`:**
```python
BROWSER_MEMORY_BOUND_MB = 300  # RAM cap for browser instance
BROWSER_SNAPSHOT_DIR = "data/snapshots/browser"  # Screenshot storage
BROWSER_VIEWPORT = (1280, 720)  # Default viewport dimensions
BROWSER_TIMEOUT_SECONDS = 30.0  # Navigation timeout
```

## Strict System Bounds

- **Total RAM footprint:** < 1.5 GB across all processes
- **Playwright instance:** < 300 MB (enforced via resource throttling)
- **Self-Codebase Protection:** Blocks mutations to `robber/`, `gateway/`, `indexer/`, `ui/`
- **SQLite SSOT:** Preserves WAL mode database state
- **IPC WebSocket:** Maintains streaming connections during tests

## Examples

### Example 1: Clean Render Test
```bash
/webtest http://example.com
```
Output:
```
[WEBTEST] Initializing browser automation for http://example.com
[WEBTEST] Navigating to http://example.com
[WEBTEST] Captured screenshot: data/snapshots/browser/20260906_073000_example.png
[WEBTEST] Status: 200, DOM nodes: 1256
[WEBTEST] ✓ Clean render — no console errors or network failures
```

### Example 2: Detect and Heal JS Crash
```bash
/webtest http://localhost:3000
```
Output:
```
[WEBTEST] Starting dev server: npm run dev
[WEBTEST] Dev server launched (PID 12345)
[WEBTEST] Waiting for port 3000 to bind...
[WEBTEST] Port 3000 is ready
[WEBTEST] Navigating to http://localhost:3000
[WEBTEST] ✗ Found 2 issue(s)
  1. Console error: Cannot read properties of undefined (reading 'userId')
  2. Failed GET /api/user
[WEBTEST] Triggering VisionAuditorWorker for self-healing...
[VISION_AUDITOR] Analyzing traceback: src/components/UserProfile.tsx:42
[VISION_AUDITOR] Synthesizing AST fix: add optional chaining ?.userId
[VISION_AUDITOR] Re-testing page...
[WEBTEST] ✓ Self-healing succeeded
```

## API Reference

### `PlaywrightBrowserEngine`

```python
from robber.tools.browser_engine import PlaywrightBrowserEngine

engine = PlaywrightBrowserEngine(workspace_root)
snapshot = await engine.navigate_and_capture("http://localhost:3000")
await engine.close()
```

### `BrowserInspector`

```python
from robber.diagnostics.browser_inspector import BrowserInspector

inspector = BrowserInspector(workspace_root)
report = inspector.inspect_snapshot(snapshot)
print(f"Errors: {len(report.console_errors)}")
```

### `VisionAuditorWorker`

```python
from robber.agents.vision_auditor import VisionAuditorWorker

worker = VisionAuditorWorker(workspace_root, manager=state_manager, hub=hub)
task = {"payload_json": {"url": "http://localhost:3000", "auto_heal": True}}
result = await worker.run(task, None)
```

## Testing

Run the browser automation test suite:
```bash
pytest tests/test_browser_automation.py -v
```

Test with the fixture HTML app:
```bash
python -m http.server 8080 --directory tests/fixtures
# In another terminal:
/webtest http://localhost:8080/test_app.html
```

## Troubleshooting

### Playwright Not Found
```bash
pip install playwright>=1.40
playwright install chromium
```

### Memory Limit Exceeded
If the browser instance exceeds 300 MB, check:
- `BROWSER_MEMORY_BOUND_MB` config
- Chrome launch flags in `PlaywrightBrowserEngine._launch_browser()`

### Port Binding Timeout
If `/webtest` times out waiting for dev server:
- Check if port is already bound: `netstat -ano | findstr :3000` (Windows)
- Increase timeout in `_wait_for_port()` call
- Manually specify port: `/webtest npm run dev -- --port 3000`

### Self-Healing Fails
If `VisionAuditorWorker` cannot fix errors:
- Check `heal_frontend_issue()` logs in `robber/agents/vision_auditor.py`
- Verify file is not in protected directories (`robber/`, `gateway/`, etc.)
- Ensure source file has correct syntax before repair

## Limitations

- Only supports JavaScript/TypeScript frontend files for self-healing
- Vision model context limited to 2048 tokens (configurable via `SWARM_VISION_TOKENS`)
- Screenshot analysis requires multimodal LLM support (Gemini, Claude, Qwen-VL)
- Dev server must bind to localhost (remote URLs tested but not auto-launched)

## Future Enhancements

- [ ] Multi-page flow testing (navigation sequences)
- [ ] Visual regression testing (screenshot diffs)
- [ ] Accessibility tree validation (ARIA compliance)
- [ ] Performance profiling (Core Web Vitals)
- [ ] Cross-browser testing (Firefox, WebKit)
- [ ] Mobile viewport emulation
- [ ] Network throttling simulation
