"""Desktop entry point of the installed app (pythonw: no console window). Starts or reattaches to the local services and
opens the control centre in the default browser on http://127.0.0.1. Failures are shown in a message box with the log."""
import ctypes
import io
import sys
from contextlib import redirect_stdout


def main() -> int:
    buf = io.StringIO()
    try:
        from nexus import launcher
        with redirect_stdout(buf):
            rc = launcher.start(open_browser=True)
    except Exception as e:  # noqa: BLE001
        rc, buf = 99, io.StringIO(f"{type(e).__name__}: {e}")
    if rc != 0:
        ctypes.windll.user32.MessageBoxW(None, "NEXUS VIDEO AI could not start.\n\n" + buf.getvalue()[-1800:], "NEXUS VIDEO AI", 0x10)
    return rc


if __name__ == "__main__":
    sys.exit(main())
