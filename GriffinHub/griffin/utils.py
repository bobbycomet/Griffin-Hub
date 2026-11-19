"""Shared utilities: environment sanitising, streaming subprocesses, downloads, hashing."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import requests

# Variables the AppImage's AppRun prepends bundled paths to. Child processes
# (winetricks, Proton, games) must not inherit those or they may load our
# bundled libraries instead of their own.
_PATH_LIKE_VARS = (
    "PATH",
    "LD_LIBRARY_PATH",
    "XDG_DATA_DIRS",
    "PYTHONPATH",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "GIO_EXTRA_MODULES",
)


def clean_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Return a copy of the environment without AppImage-bundled paths.

    When running from source (no ``GRIFFIN_APPDIR``) this is just a copy.
    """
    env = dict(os.environ if base is None else base)
    appdir = env.pop("GRIFFIN_APPDIR", "")
    if not appdir:
        return env
    appdir = appdir.rstrip("/")

    def _outside(entry: str) -> bool:
        return not (entry == appdir or entry.startswith(appdir + "/"))

    for var in _PATH_LIKE_VARS:
        if var in env:
            kept = [e for e in env[var].split(os.pathsep) if e and _outside(e)]
            if kept:
                env[var] = os.pathsep.join(kept)
            else:
                env.pop(var, None)
    home = env.get("PYTHONHOME", "")
    if home and not _outside(home):
        env.pop("PYTHONHOME", None)
    return env


def slugify(text: str, fallback: str = "game") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-._").lower()
    return s[:60] or fallback


def windows_path(unix_path: str | Path) -> str:
    """Map a Unix path to the Z: drive path Wine uses (``/home/a`` → ``Z:\\home\\a``)."""
    return "Z:" + str(unix_path).replace("/", "\\")


def atomic_write_bytes(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Write ``data`` to ``path`` via a temp file + rename (never leaves a half-written file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(
    url: str,
    path: Path,
    timeout: int = 300,
    *,
    expected_sha256: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> None:
    """Download to ``path`` atomically. Optionally enforce a size cap and SHA-256."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    h = hashlib.sha256()
    total = 0
    try:
        with requests.get(
            url, stream=True, timeout=timeout, headers={"User-Agent": "GriffinGameTweaks/1.1"}
        ) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise RuntimeError(f"Download exceeds size limit ({max_bytes} bytes): {url}")
                    h.update(chunk)
                    f.write(chunk)
        if expected_sha256 and h.hexdigest().lower() != expected_sha256.lower():
            raise RuntimeError(
                f"Checksum mismatch for {url}\n  expected {expected_sha256}\n  got      {h.hexdigest()}"
            )
        os.replace(part, path)
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def _terminate(proc: subprocess.Popen) -> None:
    """Stop a process *and its children* (we start each child in its own session)."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)


def run_streaming(
    cmd: Sequence[str | Path],
    *,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str | Path] = None,
    on_line: Optional[Callable[[str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    timeout: Optional[float] = None,
) -> int:
    """Run ``cmd``, stream its combined output line-by-line to ``on_line``.

    * Cancellation (``cancel()`` returning True) terminates the whole process group
      and raises ``RuntimeError("Cancelled")``.
    * ``timeout`` (seconds) raises ``subprocess.TimeoutExpired``.
    * Returns the exit code.
    """
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line and on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    started = time.monotonic()
    try:
        while True:
            ret = proc.poll()
            if ret is not None:
                break
            if cancel and cancel():
                _terminate(proc)
                raise RuntimeError("Cancelled")
            if timeout is not None and time.monotonic() - started > timeout:
                _terminate(proc)
                raise subprocess.TimeoutExpired([str(c) for c in cmd], timeout)
            time.sleep(0.2)
    finally:
        # wineserver may keep the pipe open a moment after the parent exits
        t.join(1.0)
    return ret
