from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ProcessHandle:
    name: str
    popen: subprocess.Popen
    log_path: str
    cwd: str


def _ensure_parent(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def start_process(
    *,
    name: str,
    args: List[str],
    cwd: str,
    log_path: str,
    env_overrides: Optional[Dict[str, str]] = None,
) -> ProcessHandle:
    """
    Start a subprocess and stream stdout/stderr to `log_path`.
    """
    _ensure_parent(log_path)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                continue
            env[str(k)] = str(v)

    # Avoid relying on "python" being in PATH.
    if args and args[0] == "python":
        args = [sys.executable] + args[1:]

    f = open(log_path, "a", encoding="utf-8")
    try:
        ts = datetime.now(timezone.utc).isoformat()
        f.write(f"[dashboard] start_process name={name} ts_utc={ts}\n")
        f.write(f"[dashboard] cwd={cwd}\n")
        f.write(f"[dashboard] argv={' '.join(map(str, args))}\n")
        f.flush()
    except Exception:
        pass
    p = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdout=f,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return ProcessHandle(name=name, popen=p, log_path=str(log_path), cwd=str(cwd))


def stop_process(h: ProcessHandle, *, timeout_seconds: float = 3.0) -> None:
    """
    Best-effort stop.
    """
    p = h.popen
    if p.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.kill(p.pid, signal.SIGTERM)
        else:
            p.terminate()
        p.wait(timeout=timeout_seconds)
        return
    except Exception:
        pass
    try:
        if os.name == "posix":
            os.kill(p.pid, signal.SIGKILL)
        else:
            p.kill()
    except Exception:
        pass


def read_log_tail(path: str, *, max_bytes: int = 20_000) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        data = p.read_bytes()
    except Exception:
        return ""
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="ignore")
    return data[-max_bytes:].decode("utf-8", errors="ignore")

