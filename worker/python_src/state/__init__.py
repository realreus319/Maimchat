from __future__ import annotations

from .store import Store
from .app_state_store import (
    AppState,
    AppStateValidationIssue,
    get_default_app_state,
    create_app_state_store,
    normalize_app_state_in_place,
)


def load_persisted_state(*args, **kwargs):
    from .persistence import load_persisted_state as _load_persisted_state

    return _load_persisted_state(*args, **kwargs)


def save_persisted_state(*args, **kwargs):
    from .persistence import save_persisted_state as _save_persisted_state

    return _save_persisted_state(*args, **kwargs)


def clear_persisted_state(*args, **kwargs):
    from .persistence import clear_persisted_state as _clear_persisted_state

    return _clear_persisted_state(*args, **kwargs)

__all__ = [
    "Store",
    "AppState",
    "AppStateValidationIssue",
    "get_default_app_state",
    "create_app_state_store",
    "normalize_app_state_in_place",
    "load_persisted_state",
    "save_persisted_state",
    "clear_persisted_state",
]
