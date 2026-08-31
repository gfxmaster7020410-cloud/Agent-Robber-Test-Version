# Agent Robber E2E Integration Checklist

## 1. Prerequisites (Run in Separate Terminals)
- **Backend:** `npm run daemon` (starts `python server.py`)
- **Front-end UI:** `npm run gui` (starts development server)
- **CLI TUI:** `npm run tui`

## 2. Automated Validation
- Run the test script: `python scripts/test_live_system.py`

## 3. Live Integration Checklist
- [ ] **Daemon Boot:** No errors in `npm run daemon` console output.
- [ ] **WebSocket Sync:** `npm run tui` reflects real-time status updates from the daemon.
- [ ] **UI Connection:** `localhost:5173` renders the dashboard with "Connected" status.
- [ ] **Task Execution:** Triggering a prompt in TUI/GUI initiates an agent task loop (verified via logs).
- [ ] **Browser Automation:** A stealth browser window launches when an `act` task runs.
- [ ] **Data Logging:** Files in `data/` or `session-*.md` updated with task artifacts.

If any step fails, check `server.py` logs or verify `.env` configuration.
