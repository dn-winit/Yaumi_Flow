"""Start all 5 backend services + the webapp in correct dependency order.

Usage:
    python scripts/serve_local.py               # start everything; Ctrl-C stops everything
    python scripts/serve_local.py --skip-webapp # backends only

Each service is a separate subprocess. The launcher waits for each
service's /health endpoint to return 200 before starting the next, so
downstream services never race their dependencies. Logs are streamed
with a per-service prefix. Ctrl-C (SIGINT) shuts every subprocess down
cleanly. If any service exits unexpectedly, the launcher tears the
rest down and exits non-zero -- mirrors Railway's "one bad process
fails the deploy" behaviour, so local dev matches production.
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent

# (label, module path, port, health URL) -- order is the start order.
SERVICES = [
    ("data_import",         "data_import",                   8005, "/api/v1/data/health"),
    ("demand_forecasting",  "demand_forecasting_pipeline",   8002, "/api/v1/forecast/health"),
    ("recommended_order",   "recommended_order",             8001, "/api/v1/recommended-order/summary"),
    ("llm_analytics",       "llm_analytics",                 8003, "/api/v1/analytics/health"),
    ("sales_supervision",   "sales_supervision",             8004, "/api/v1/supervision/health"),
]
WEBAPP_DIR = REPO / "webapp"


def _python_exe() -> str:
    """Prefer the project's .venv interpreter; fall back to PATH ``python``."""
    candidates = [
        REPO / ".venv" / "Scripts" / "python.exe",   # Windows venv
        REPO / ".venv" / "bin" / "python",           # POSIX venv
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def _stream(proc: subprocess.Popen, label: str) -> None:
    """Pipe a subprocess's stdout to the parent with a ``[label]`` prefix."""
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"[{label:<20s}] {line}")
        sys.stdout.flush()


def _wait_healthy(label: str, port: int, path: str, timeout: float = 60.0) -> bool:
    """Poll the health endpoint until 200 or timeout."""
    deadline = time.time() + timeout
    url = f"http://localhost:{port}{path}"
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=3.0) as c:
                if c.get(url).status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    print(f"[launcher           ] {label} did not become healthy within {timeout:.0f}s", file=sys.stderr)
    return False


def _launch_backend(label: str, module: str) -> subprocess.Popen:
    return subprocess.Popen(
        [_python_exe(), "-m", module],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )


def _launch_webapp() -> subprocess.Popen:
    npm = "npm.cmd" if sys.platform.startswith("win") else "npm"
    return subprocess.Popen(
        [npm, "run", "dev"],
        cwd=str(WEBAPP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-webapp", action="store_true")
    ap.add_argument("--health-timeout", type=float, default=60.0)
    args = ap.parse_args()

    procs: list[tuple[str, subprocess.Popen]] = []
    threads: list[threading.Thread] = []

    def shutdown(*_: object) -> None:
        print("\n[launcher           ] SIGINT received; stopping all services...")
        for _label, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for _label, p in reversed(procs):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        for label, module, port, health in SERVICES:
            print(f"[launcher           ] starting {label} ...")
            p = _launch_backend(label, module)
            procs.append((label, p))
            t = threading.Thread(target=_stream, args=(p, label), daemon=True)
            t.start()
            threads.append(t)
            if not _wait_healthy(label, port, health, args.health_timeout):
                shutdown()
                return 1
            print(f"[launcher           ] {label} healthy on :{port}")

        if not args.skip_webapp:
            print("[launcher           ] starting webapp ...")
            w = _launch_webapp()
            procs.append(("webapp", w))
            threading.Thread(target=_stream, args=(w, "webapp"), daemon=True).start()

        # Block until any process exits.
        while True:
            for label, p in procs:
                if p.poll() is not None:
                    print(f"[launcher           ] {label} exited with code {p.returncode}; tearing down")
                    shutdown()
                    return p.returncode or 1
            time.sleep(1.0)
    except Exception as exc:
        print(f"[launcher           ] fatal: {exc}", file=sys.stderr)
        shutdown()
        return 1


if __name__ == "__main__":
    sys.exit(main())
