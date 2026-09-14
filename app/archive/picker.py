"""Native folder picker for the local UI.

A browser can never hand a server a real filesystem path - but here the server
runs on the same machine as the browser, so it opens the dialog itself.

Tk must own the main thread on Windows, and the API server already owns it, so
the dialog runs in a short-lived subprocess that prints the chosen path.
"""

from __future__ import annotations

import subprocess
import sys

from . import config

# The path comes back as raw UTF-8 bytes rather than through print(). Windows
# consoles are cp1250 here, so a printed "G:\\Můj disk" would reach the parent
# as "G:\\MĹŻj disk" - a path that does not exist.
_SCRIPT = """
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.{call}
root.destroy()
sys.stdout.buffer.write((path or "").encode("utf-8"))
"""

_DB_TYPES = '[("Archive database", "*.sqlite *.db *.sqlite3"), ("All files", "*.*")]'
# Two exports arrive as a single file rather than a folder: Discord History
# Tracker's own database, and the tar a Microsoft Teams export downloads as.
_EXPORT_TYPES = (
    '[("Export file", "*.dht *.tar"), ("Discord History Tracker", "*.dht"), '
    '("Microsoft Teams export", "*.tar"), ("All files", "*.*")]'
)


def _ask(call: str, timeout: int) -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT.format(call=call)],
            capture_output=True,  # bytes, decoded below - never the console codepage
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "the file dialog failed")
    return result.stdout.decode("utf-8").strip() or None


def ask_directory(timeout: int = 300) -> str | None:
    """Open a folder chooser. Returns the path, or None if cancelled."""
    return _ask('askdirectory(title="Select the exported chat folder")', timeout)


def ask_export_file(timeout: int = 300) -> str | None:
    """Open a file chooser for an export that comes as one file.

    A Discord History Tracker `.dht`, or the `.tar` a Microsoft Teams export
    downloads as - which is read where it lies rather than unpacked, so there is
    nothing to pick but the file itself.
    """
    return _ask(
        f'askopenfilename(title="Select an export file", filetypes={_EXPORT_TYPES})',
        timeout,
    )


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
        f'initialfile="{config.NEW_DB_NAME}", filetypes={_DB_TYPES})',
        timeout,
    )
