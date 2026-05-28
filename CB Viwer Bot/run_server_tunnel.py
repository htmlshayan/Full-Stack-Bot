"""Run CB Viewer server and Cloudflare tunnel together."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.is_file():
    load_dotenv(ENV_PATH)


def _require_cloudflared() -> None:
    if not shutil.which("cloudflared"):
        raise SystemExit("cloudflared not found in PATH. Install it first.")


def _resolve_token(cli_token: str) -> str:
    token = (cli_token or "").strip() or os.environ.get("CLOUDFLARED_TUNNEL_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing Cloudflare tunnel token. Use --token or set CLOUDFLARED_TUNNEL_TOKEN.")
    return token


def _start_process(command: list, name: str) -> subprocess.Popen:
    print(f"[INFO] Starting {name}...")
    return subprocess.Popen(command, cwd=str(PROJECT_ROOT))


def _stop_process(proc: subprocess.Popen, name: str) -> None:
    if not proc or proc.poll() is not None:
        return
    print(f"[INFO] Stopping {name}...")
    try:
        proc.terminate()
        proc.wait(timeout=10)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CB Viewer server and Cloudflare tunnel.")
    parser.add_argument("--token", default="", help="Cloudflare tunnel token")
    args = parser.parse_args()

    _require_cloudflared()
    token = _resolve_token(args.token)

    server_cmd = [sys.executable, "main.py"]
    tunnel_cmd = ["cloudflared", "tunnel", "run", "--token", token]

    server_proc = _start_process(server_cmd, "CB Viewer server")
    tunnel_proc = _start_process(tunnel_cmd, "Cloudflare tunnel")

    def _handle_stop(_sig, _frame):
        _stop_process(tunnel_proc, "Cloudflare tunnel")
        _stop_process(server_proc, "CB Viewer server")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_stop)
    for sig_name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_stop)
        except (ValueError, OSError, RuntimeError):
            pass

    try:
        while True:
            if server_proc.poll() is not None:
                raise SystemExit(f"Server exited with code {server_proc.returncode}")
            if tunnel_proc.poll() is not None:
                raise SystemExit(f"Tunnel exited with code {tunnel_proc.returncode}")
            time.sleep(1)
    finally:
        _stop_process(tunnel_proc, "Cloudflare tunnel")
        _stop_process(server_proc, "CB Viewer server")


if __name__ == "__main__":
    main()
