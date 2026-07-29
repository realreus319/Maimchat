"""AppState change handler — mirrors src/state/onChangeAppState.ts.

When fields that affect global config change, this handler persists
them back to ``~/.claude_py/claude.json`` and fires side effects like
clearing auth caches and re-applying env vars.

For the Task 28 Linux port, we implement the subset of side effects that
don't depend on Ink/TUI, GrowthBook, or analytics:
- verbose → global config
- expanded_view → global config (showExpandedTodos, showSpinnerTree)
- main_loop_model → settings + bootstrap override
- settings change → clear auth caches (stub for now)
"""

from __future__ import annotations

from collections.abc import Mapping

from .app_state_store import AppState
from ..utils.hooks import dispatch_hook_event_sync


def handle_app_state_change(
    new_state: AppState,
    old_state: AppState | None,
) -> None:
    previous_state = old_state if isinstance(old_state, AppState) else AppState()
    if new_state.verbose != previous_state.verbose:
        _persist_verbose(new_state.verbose)

    if new_state.expanded_view != previous_state.expanded_view:
        _persist_expanded_view(new_state.expanded_view)

    if new_state.main_loop_model != previous_state.main_loop_model:
        if new_state.main_loop_model is not None:
            _persist_main_loop_model(new_state.main_loop_model)
        else:
            _clear_main_loop_model()

    if new_state.settings != previous_state.settings:
        _on_settings_change(new_state, previous_state)


def _persist_verbose(verbose: bool) -> None:
    from ..utils.config import get_global_config, save_global_config

    if get_global_config().get("verbose") != verbose:
        save_global_config(lambda current: {**current, "verbose": verbose})


def _persist_expanded_view(expanded_view: str) -> None:
    from ..utils.config import get_global_config, save_global_config

    show_expanded_todos = expanded_view == "tasks"
    show_spinner_tree = expanded_view == "teammates"
    config = get_global_config()
    if (
        config.get("showExpandedTodos") != show_expanded_todos
        or config.get("showSpinnerTree") != show_spinner_tree
    ):
        save_global_config(
            lambda current: {
                **current,
                "showExpandedTodos": show_expanded_todos,
                "showSpinnerTree": show_spinner_tree,
            }
        )


def _persist_main_loop_model(model: str) -> None:
    from ..bootstrap import setMainLoopModelOverride
    from ..utils.settings.settings import update_settings_for_source

    update_settings_for_source("userSettings", {"model": model})
    setMainLoopModelOverride(model)


def _clear_main_loop_model() -> None:
    from ..bootstrap import setMainLoopModelOverride
    from ..utils.settings.settings import update_settings_for_source

    update_settings_for_source("userSettings", {"model": None})
    setMainLoopModelOverride(None)


def _on_settings_change(
    new_state: AppState,
    old_state: AppState,
) -> None:
    from ..utils.plugin_registry import sync_managed_plugins_runtime_state
    from ..utils.settings.settings_cache import reset_settings_cache

    reset_settings_cache()
    sync_managed_plugins_runtime_state(new_state, force=True)
    changed_paths = _diff_mapping_paths(old_state.settings, new_state.settings)
    if not changed_paths:
        return
    payload = {
        "cwd": _hook_cwd(new_state),
        "changedKeys": sorted({path.split(".", 1)[0] for path in changed_paths}),
        "changedPaths": changed_paths,
        "oldSettings": dict(old_state.settings),
        "newSettings": dict(new_state.settings),
    }
    dispatch_hook_event_sync(
        "ConfigChange",
        payload,
        app_state=new_state,
        cwd=_hook_cwd(new_state),
    )


def _hook_cwd(app_state: AppState) -> str | None:
    tool_context = getattr(app_state, "tool_permission_context", None)
    if not isinstance(tool_context, Mapping):
        return None
    cwd = tool_context.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    return None


def _diff_mapping_paths(
    old_mapping: Mapping[str, object],
    new_mapping: Mapping[str, object],
    *,
    prefix: str = "",
) -> list[str]:
    changed: list[str] = []
    keys = set(old_mapping) | set(new_mapping)
    for key in sorted(keys):
        path = f"{prefix}.{key}" if prefix else key
        old_value = old_mapping.get(key)
        new_value = new_mapping.get(key)
        if isinstance(old_value, Mapping) and isinstance(new_value, Mapping):
            nested = _diff_mapping_paths(old_value, new_value, prefix=path)
            if nested:
                changed.extend(nested)
            continue
        if old_value != new_value:
            changed.append(path)
    return changed
