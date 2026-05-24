import os
import sys
import shutil
import traceback

# ── Always wipe __pycache__ before importing anything else ───────────────────
# Prevents stale bytecode from running when source files have been updated.
_log_dir = os.path.dirname(os.path.abspath(__file__))
shutil.rmtree(os.path.join(_log_dir, "__pycache__"), ignore_errors=True)

os.chdir(_log_dir)   # ensure CWD is the project folder regardless of how Python was launched
_stderr_log = open(os.path.join(_log_dir, "stderr.log"), "w", buffering=1)
sys.stderr = _stderr_log

def _excepthook(exctype, value, tb):
    msg = "".join(traceback.format_exception(exctype, value, tb))
    with open(os.path.join(_log_dir, "crash.log"), "a") as f:
        f.write(msg + "\n")
    _stderr_log.write(msg + "\n")
    _stderr_log.flush()
    sys.__excepthook__(exctype, value, tb)

sys.excepthook = _excepthook

# ── Log environment info ─────────────────────────────────────────────────────
with open(os.path.join(_log_dir, "crash.log"), "w") as f:
    f.write(f"Python: {sys.version}\n")
    f.write(f"Executable: {sys.executable}\n")
    f.write(f"CWD: {os.getcwd()}\n")

import matplotlib
matplotlib.use("QtAgg")

from PyQt6.QtWidgets import QApplication
from window import MainWindow


def main():
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("YB Race Tracker")

        win = MainWindow()
        win.show()

        code = app.exec()
        with open(os.path.join(_log_dir, "crash.log"), "a") as f:
            f.write(f"app.exec() returned {code}\n")
        sys.exit(code)

    except Exception:
        tb = traceback.format_exc()
        with open(os.path.join(_log_dir, "crash.log"), "a") as f:
            f.write("EXCEPTION IN main():\n" + tb + "\n")
        _stderr_log.write(tb)
        _stderr_log.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
