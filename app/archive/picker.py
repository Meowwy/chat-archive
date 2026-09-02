"""Native folder picker for the local UI.

A browser can never hand a server a real filesystem path - but here the server
runs on the same machine as the browser, so it opens the dialog itself.

Tk must own the main thread on Windows, and the API server already owns it, so
the dialog runs in a short-lived subprocess that prints the chosen path.
"""

from __future__ import annotations

import subprocess
import sys

_SCRIPT = """
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.{call}
root.destroy()
print(path or "")
"""

_DB_TYPES = '[("Archive database", "*.sqlite *.db *.sqlite3"), ("All files", "*.*")]'


def _ask(call: str, timeout: int) -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT.format(call=call)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "the file dialog failed")
    path = result.stdout.strip()
    return path or None


def ask_directory(timeout: int = 300) -> str | None:
    """Open a folder chooser. Returns the path, or None if cancelled."""
    return _ask('askdirectory(title="Select the exported chat folder")', timeout)


def ask_database(timeout: int = 300) -> str | None:
    """Open a file chooser for an existing archive database."""
    return _ask(
        f'askopenfilename(title="Select an archive database", filetypes={_DB_TYPES})',
        timeout,
    )


def ask_new_database(timeout: int = 300) -> str | None:
    """Ask where to put a new, empty archive database."""
    return _ask(
        'asksaveasfilename(title="Create a new archive", defaultextension=".sqlite", '
        f'initialfile="chat_archive.sqlite", filetypes={_DB_TYPES})',
        timeout,
    )
