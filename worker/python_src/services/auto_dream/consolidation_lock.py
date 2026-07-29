from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


LOCK_FILE_NAME = ".consolidate-lock"
LOCK_STALE_SECONDS = 3600.0


class ConsolidationLock:
    def __init__(self, memory_dir: str | os.PathLike[str]) -> None:
        self.memory_dir = Path(memory_dir)
        self.path = self.memory_dir / LOCK_FILE_NAME
        self.pid = os.getpid()
        self.acquired = False

    def last_consolidated_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def acquire(self) -> bool:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._is_active_lock():
            return False
        payload = {"pid": self.pid, "state": "running", "startedAt": time.time()}
        try:
            with open(self.path, "x", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
        except FileExistsError:
            if self._is_active_lock():
                return False
            self.path.unlink(missing_ok=True)
            with open(self.path, "x", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
        self.acquired = True
        return True

    def complete(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        payload = {"pid": self.pid, "state": "completed", "completedAt": time.time()}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self.acquired = False

    def rollback(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def _is_active_lock(self) -> bool:
        payload = self._read_payload()
        if payload.get("state") != "running":
            return False
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if age > LOCK_STALE_SECONDS:
            return False
        pid = payload.get("pid")
        return isinstance(pid, int) and _pid_is_running(pid)

    def _read_payload(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
