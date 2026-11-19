"""Background install task context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from griffin.utils import run_streaming


@dataclass
class TaskContext:
    status_callback: Callable[[str], None]
    log_callback: Callable[[str], None]
    progress_callback: Callable[[float], None]
    cancel_flag: Callable[[], bool]

    def set_status(self, msg: str) -> None:
        self.status_callback(msg)

    def log(self, msg: str) -> None:
        self.log_callback(msg)

    def set_progress(self, p: float) -> None:
        self.progress_callback(p)

    def is_cancelled(self) -> bool:
        return self.cancel_flag()

    def run_cancellable(
        self,
        cmd: Sequence[str],
        env: Optional[Mapping[str, str]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> int:
        """Run a command, stream its output to the log, and kill it (and its children) on cancel."""
        return run_streaming(
            cmd, env=env, on_line=self.log_callback, cancel=self.cancel_flag, timeout=timeout
        )
