"""Shim so `python main.py` launches the native TUI (there is no other main.py)."""

from robber.tui.app import main

if __name__ == "__main__":
    main()
