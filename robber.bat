@echo off
setlocal
cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

REM TUI entry: python -m robber.tui  (also tui.py / main.py)
python tui.py %*
