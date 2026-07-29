from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...services.mcp.client import clear_connection_cache
from ...tools.web_fetch import clear_web_fetch_caches
from .compact_warning_state import suppress_compact_warning


@dataclass(frozen=True)
class PostCompactCleanupResult:
    main_thread_cleanup: bool
    cleared_bound_session_messages: bool = False
    cleared_prompt_cache_detection_state: bool = False
    cleared_web_fetch_caches: bool = False
    cleared_mcp_connection_cache: bool = False


def is_main_thread_compact(query_source: str | None = None) -> bool:
    if query_source is None:
        return True
    return query_source.startswith("repl_main_thread") or query_source == "sdk"


def run_post_compact_cleanup(
    query_source: str | None = None,
    *,
    tool_executor: Any | None = None,
    model_adapter: Any | None = None,
) -> PostCompactCleanupResult:
    suppress_compact_warning()

    cleared_bound_session_messages = False
    bind_session_messages = getattr(tool_executor, "bind_session_messages", None)
    if callable(bind_session_messages):
        previous_messages = getattr(tool_executor, "session_messages", ())
        bind_session_messages(())
        cleared_bound_session_messages = bool(previous_messages)

    cleared_prompt_cache_detection_state = False
    if model_adapter is not None and hasattr(model_adapter, "_prompt_cache_detection_state"):
        cleared_prompt_cache_detection_state = (
            getattr(model_adapter, "_prompt_cache_detection_state", None) is not None
        )
        setattr(model_adapter, "_prompt_cache_detection_state", None)

    main_thread_cleanup = is_main_thread_compact(query_source)
    cleared_web_fetch_caches = False
    cleared_mcp_connection_cache = False
    if main_thread_cleanup:
        clear_web_fetch_caches()
        cleared_web_fetch_caches = True

        owner_id = getattr(tool_executor, "mcp_owner_id", None)
        if isinstance(owner_id, str) and owner_id:
            clear_connection_cache(owner_id=owner_id)
            cleared_mcp_connection_cache = True

    return PostCompactCleanupResult(
        main_thread_cleanup=main_thread_cleanup,
        cleared_bound_session_messages=cleared_bound_session_messages,
        cleared_prompt_cache_detection_state=cleared_prompt_cache_detection_state,
        cleared_web_fetch_caches=cleared_web_fetch_caches,
        cleared_mcp_connection_cache=cleared_mcp_connection_cache,
    )
