from .auto_dream import (
    AutoDreamResult,
    DreamTask,
    list_sessions_touched_since,
    make_dream_progress_watcher,
    maybe_run_auto_dream,
)
from .config import AutoDreamConfig, load_auto_dream_config
from .consolidation_lock import ConsolidationLock
from .consolidation_prompt import build_consolidation_prompt

__all__ = [
    "AutoDreamConfig",
    "AutoDreamResult",
    "ConsolidationLock",
    "DreamTask",
    "build_consolidation_prompt",
    "list_sessions_touched_since",
    "load_auto_dream_config",
    "make_dream_progress_watcher",
    "maybe_run_auto_dream",
]
