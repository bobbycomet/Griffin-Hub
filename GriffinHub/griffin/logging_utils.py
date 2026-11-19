"""Structured logging for Griffin Game Tweaks."""

from __future__ import annotations

import os
import platform
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

_log_file: Optional[TextIO] = None
_log_path: Optional[Path] = None
_lock = threading.Lock()


def default_log_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "griffin-game-tweaks" / "logs"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "griffin-game-tweaks" / "logs"
    return Path.home() / ".local" / "share" / "griffin-game-tweaks" / "logs"


def _detect_distro() -> str:
    try:
        data = Path("/etc/os-release").read_text(encoding="utf-8")
        for line in data.splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _detect_gpu() -> str:
    try:
        out = subprocess.check_output(["lspci"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "VGA" in line or "3D" in line or "Display" in line:
                return line.split(":", 2)[-1].strip()[:80]
    except Exception:
        pass
    return "Unknown"


def init_logger(log_dir: Optional[Path] = None) -> Path:
    global _log_file, _log_path
    if log_dir is None:
        log_dir = default_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = Path("/tmp") / "griffin-game-tweaks-logs"
        log_dir.mkdir(parents=True, exist_ok=True)

    logs = sorted(log_dir.glob("griffin_*.log"))
    while len(logs) >= 10:
        try:
            logs[0].unlink()
        except OSError:
            pass
        logs = logs[1:]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_path = log_dir / f"griffin_{ts}.log"
    try:
        _log_file = open(_log_path, "a", encoding="utf-8")
    except OSError:
        _log_path = Path("/tmp") / f"griffin_{ts}.log"
        _log_file = open(_log_path, "a", encoding="utf-8")

    try:
        from griffin import __version__
        version = __version__
    except Exception:
        version = "unknown"

    header = f"""================================================================================
Griffin Game Tweaks Log - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
================================================================================
Application:   Griffin Game Tweaks v{version}
Author:        Bobby Comet
System Info:
  Distro:      {_detect_distro()}
  Kernel:      {platform.release()}
  Python:      {platform.python_version()}
  Session:     {os.environ.get('XDG_SESSION_TYPE', 'Unknown')}
  Desktop:     {os.environ.get('XDG_CURRENT_DESKTOP', 'Unknown')}
  GPU:         {_detect_gpu()}
================================================================================
"""
    _write_raw(header)
    print(f"Log file: {_log_path}")
    return _log_path


def _write_raw(msg: str, *, echo: bool = True) -> None:
    with _lock:
        if _log_file:
            try:
                _log_file.write(msg if msg.endswith("\n") else msg + "\n")
                _log_file.flush()
            except OSError:
                pass
        if echo:
            try:
                print(msg.rstrip())
            except (OSError, ValueError):
                pass


def _log(level: str, message: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    _write_raw(f"[{ts}] [{level}] {message}")


def log_debug(msg: str) -> None:
    """Write to the log file only (not the console) — used for verbose tool output."""
    ts = datetime.now().strftime("%H:%M:%S")
    _write_raw(f"[{ts}] [DEBUG] {msg}", echo=False)


def get_log_path() -> Optional[Path]:
    return _log_path


def log_info(msg: str) -> None:
    _log("INFO", msg)


def log_action(msg: str) -> None:
    _log("ACTION", msg)


def log_download(msg: str) -> None:
    _log("DOWNLOAD", msg)


def log_install(msg: str) -> None:
    _log("INSTALL", msg)


def log_warning(msg: str) -> None:
    _log("WARNING", msg)


def log_error(msg: str) -> None:
    _log("ERROR", msg)
