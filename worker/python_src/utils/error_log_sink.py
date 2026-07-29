from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import get_claude_config_home


def _default_config_home() -> str:
    return get_claude_config_home()


def date_to_filename(when: datetime) -> str:
    return when.strftime("%Y-%m-%d")


def _safe_server_name(server_name: str) -> str:
    return server_name.replace("/", "_").replace("\\", "_")


def get_errors_path(
    *,
    config_home: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    base = config_home or _default_config_home()
    stamp = date_to_filename(now or datetime.now())
    return str(Path(base) / "errors" / f"{stamp}.jsonl")


def get_mcp_logs_path(
    server_name: str,
    *,
    config_home: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    base = config_home or _default_config_home()
    stamp = date_to_filename(now or datetime.now())
    return str(
        Path(base) / "mcp-logs" / _safe_server_name(server_name) / f"{stamp}.jsonl"
    )


@dataclass(frozen=True)
class DiagnosticContext:
    session_id: str = "default-session"
    cwd: str = ""
    version: str = "python-port"
    user_type: str = "external"


class LocalErrorLogSink:
    def __init__(
        self,
        *,
        config_home: Optional[str] = None,
        context: Optional[DiagnosticContext] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config_home = config_home or _default_config_home()
        self._context = context or DiagnosticContext(cwd=os.getcwd())
        self._now_fn = now_fn or datetime.now

    def log_error(self, error: BaseException) -> str:
        error_text = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ).strip()
        payload = {
            "timestamp": self._timestamp(),
            "error": error_text,
            "cwd": self._context.cwd,
            "userType": self._context.user_type,
            "sessionId": self._context.session_id,
            "version": self._context.version,
        }
        path = get_errors_path(config_home=self._config_home, now=self._now_fn())
        self._append_jsonl(path, payload)
        return path

    def log_mcp_error(self, server_name: str, error: object) -> str:
        if isinstance(error, BaseException):
            error_text = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ).strip()
        else:
            error_text = str(error)
        payload = {
            "timestamp": self._timestamp(),
            "error": error_text,
            "cwd": self._context.cwd,
            "sessionId": self._context.session_id,
        }
        path = get_mcp_logs_path(
            server_name,
            config_home=self._config_home,
            now=self._now_fn(),
        )
        self._append_jsonl(path, payload)
        return path

    def log_mcp_debug(self, server_name: str, message: str) -> str:
        payload = {
            "timestamp": self._timestamp(),
            "debug": message,
            "cwd": self._context.cwd,
            "sessionId": self._context.session_id,
        }
        path = get_mcp_logs_path(
            server_name,
            config_home=self._config_home,
            now=self._now_fn(),
        )
        self._append_jsonl(path, payload)
        return path

    def _timestamp(self) -> str:
        return self._now_fn().isoformat()

    @staticmethod
    def _append_jsonl(path: str, payload: object) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def initialize_error_log_sink(
    *,
    config_home: Optional[str] = None,
    context: Optional[DiagnosticContext] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> LocalErrorLogSink:
    return LocalErrorLogSink(
        config_home=config_home,
        context=context,
        now_fn=now_fn,
    )


__all__ = [
    "DiagnosticContext",
    "LocalErrorLogSink",
    "date_to_filename",
    "get_errors_path",
    "get_mcp_logs_path",
    "initialize_error_log_sink",
]
