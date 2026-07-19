"""
Convenience launcher for the Invisible Interview Assistant.

Running this file starts both the FastAPI backend
(`python -m backend.main`) and the PyQt5 overlay
(`python -m frontend.overlay`) as separate child processes,
so you can launch the whole app with a single command.

Both processes share stdout/stderr with this launcher. Press
Ctrl+C to stop both.

Usage:
    python main.py
"""
from __future__ import annotations

import signal
import subprocess
import sys
from typing import List


def _spawn(label: str, args: List[str]) -> subprocess.Popen:
    """Start a child process; line-buffer its output so it shows up
    in this launcher's console in real time."""
    print(f"[launcher] starting {label}: {' '.join(args)}", flush=True)
    # bufsize=1 + text mode = line-buffered; universal_newlines=True
    # makes .readline() return strings on both Windows and POSIX.
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _pump(stream, prefix: str) -> None:
    """Forward lines from a child stream to our stdout until EOF."""
    try:
        for line in iter(stream.readline, ""):
            print(f"[{prefix}] {line}", end="")
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def main() -> int:
    py = sys.executable
    backend = _spawn("backend", [py, "-m", "backend.main"])
    overlay = _spawn("overlay", [py, "-m", "frontend.overlay"])

    import threading
    threads = [
        threading.Thread(target=_pump, args=(backend.stdout, "backend"),
                         daemon=True),
        threading.Thread(target=_pump, args=(overlay.stdout, "overlay"),
                         daemon=True),
    ]
    for t in threads:
        t.start()

    def _shutdown(signum, frame):  # noqa: ARG001
        print("\n[launcher] stopping…", flush=True)
        for proc in (overlay, backend):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    try:
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, AttributeError):
        # SIGINT handlers only work in the main thread; on Windows
        # signal.SIGTERM may also be unavailable. We still get Ctrl+C
        # via KeyboardInterrupt below.
        pass

    try:
        # Wait for either child to exit. If one dies, take the other
        # down too so we don't leave a zombie.
        while True:
            status_b = backend.poll()
            status_o = overlay.poll()
            if status_b is not None or status_o is not None:
                break
            import time
            time.sleep(0.2)
    except KeyboardInterrupt:
        _shutdown(0, None)

    for proc in (overlay, backend):
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    print("[launcher] stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
