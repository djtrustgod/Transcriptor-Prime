"""Entry point: ``python -m transcriptor_prime``.

run.bat launches this with ``pythonw.exe`` so no console window appears. That
also means a startup failure would otherwise be silent, hence the catch-all
that puts the traceback in a dialog box.
"""

from __future__ import annotations

import sys
import traceback


def main() -> int:
    try:
        from transcriptor_prime.app import run

        return run()
    except Exception:
        _report_startup_failure(traceback.format_exc())
        return 1


def _report_startup_failure(details: str) -> None:
    sys.stderr.write(details)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Transcriptor Prime — startup failed",
            "The application could not start.\n\n"
            f"{details}\n\n"
            "If dependencies are missing, delete the .venv folder and run "
            "run.bat again.",
        )
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
