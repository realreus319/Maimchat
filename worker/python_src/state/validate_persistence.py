from __future__ import annotations

import json
import os
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Callable, List


def _load_contract() -> Callable[[], tuple[bool, tuple[str, ...]]]:
    if __package__ not in (None, ""):
        return validate_persistence_contract
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return import_module(
        "python_src.state.validate_persistence"
    ).validate_persistence_contract


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_persistence_contract() -> tuple[bool, tuple[str, ...]]:
    errors: List[str] = []
    HistoryStore = import_module("python_src.history").HistoryStore
    migrations = import_module("python_src.migrations")
    MigrationContext = migrations.MigrationContext
    run_all_migrations = migrations.run_all_migrations
    get_default_app_state = import_module(
        "python_src.state.app_state_store"
    ).get_default_app_state
    persistence = import_module("python_src.state.persistence")
    clear_persisted_state = persistence.clear_persisted_state
    load_persisted_state = persistence.load_persisted_state
    save_persisted_state = persistence.save_persisted_state
    get_global_config_for_home = import_module(
        "python_src.utils.config"
    ).get_global_config_for_home
    settings_module = import_module("python_src.utils.settings.settings")
    get_settings_for_source = settings_module.get_settings_for_source
    reset_settings_cache = settings_module.reset_settings_cache

    with tempfile.TemporaryDirectory() as tmpdir:
        config_home = os.path.join(tmpdir, "home", ".claude_py")
        project_root = os.path.join(tmpdir, "project")
        os.makedirs(project_root, exist_ok=True)

        state = get_default_app_state()
        state.verbose = True
        state.expanded_view = "tasks"
        state.main_loop_model = "opus"
        state.fast_mode = True
        state.effort_value = "high"
        save_persisted_state(state, config_home=config_home, project_root=project_root)
        reset_settings_cache()
        loaded = load_persisted_state(
            config_home=config_home, project_root=project_root
        )
        if not loaded.verbose or loaded.expanded_view != "tasks":
            errors.append("persisted state did not round-trip via config-backed fields")
        if loaded.main_loop_model != "opus" or loaded.fast_mode is not True:
            errors.append("persisted settings-backed state did not round-trip")
        if loaded.effort_value != "high":
            errors.append("effortValue was not restored from user settings")

        clear_persisted_state(config_home=config_home, project_root=project_root)
        reset_settings_cache()
        cleared = load_persisted_state(
            config_home=config_home, project_root=project_root
        )
        if cleared.verbose or cleared.expanded_view != "none":
            errors.append(
                "clear_persisted_state did not restore default config-backed values"
            )
        if cleared.main_loop_model is not None or cleared.fast_mode:
            errors.append(
                "clear_persisted_state did not clear user-settings-backed values"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        config_home = os.path.join(tmpdir, "home", ".claude_py")
        project_root = os.path.join(tmpdir, "project")
        os.makedirs(config_home, exist_ok=True)
        os.makedirs(project_root, exist_ok=True)
        _write_json(
            Path(config_home) / "state.json",
            {
                "verbose": True,
                "expandedView": "teammates",
                "mainLoopModel": "sonnet",
                "fastMode": True,
                "effortValue": "max",
            },
        )
        loaded = load_persisted_state(
            config_home=config_home, project_root=project_root
        )
        if loaded.expanded_view != "teammates" or loaded.main_loop_model != "sonnet":
            errors.append("legacy state.json fallback was not honored")
        (Path(config_home) / "state.json").write_text("{broken", encoding="utf-8")
        _write_json(Path(config_home) / "claude.json", {"verbose": True})
        (Path(config_home) / "settings.json").write_text("{broken", encoding="utf-8")
        reset_settings_cache()
        recovered = load_persisted_state(
            config_home=config_home, project_root=project_root
        )
        if recovered.verbose is not True or recovered.main_loop_model is not None:
            errors.append("corrupt artifacts did not recover to safe defaults")

    with tempfile.TemporaryDirectory() as tmpdir:
        config_home = os.path.join(tmpdir, "home", ".claude_py")
        os.makedirs(config_home, exist_ok=True)
        store = HistoryStore(
            config_home=config_home, project_root="/repo", session_id="s1"
        )
        store.add_to_history("alpha")
        store.add_to_history("beta")
        store.flush()
        store.remove_last_from_history()
        history_path = Path(config_home) / "history.jsonl"
        history_path.write_text(
            history_path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8"
        )
        entries = list(store.get_history())
        if [entry["display"] for entry in entries] != ["alpha"]:
            errors.append(
                "history undo/corrupt-line recovery diverged from expected semantics"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        config_home = os.path.join(tmpdir, "home", ".claude_py")
        project_root = os.path.join(tmpdir, "project")
        os.makedirs(config_home, exist_ok=True)
        os.makedirs(project_root, exist_ok=True)
        _write_json(
            Path(config_home) / "claude.json",
            {
                "autoUpdates": False,
                "bypassPermissionsModeAccepted": True,
                "replBridgeEnabled": True,
                "numStartups": 3,
            },
        )
        _write_json(
            Path(config_home) / "settings.json",
            {
                "model": "claude-sonnet-4-5-20250929[1m]",
                "skipAutoPermissionPrompt": True,
                "permissions": {"defaultMode": "plan"},
            },
        )
        _write_json(
            Path(project_root) / ".claude_py" / "claude.json",
            {
                "enableAllProjectMcpServers": True,
                "enabledMcpjsonServers": ["a"],
                "disabledMcpjsonServers": ["b"],
            },
        )
        migration_errors = run_all_migrations(
            config_home=config_home,
            project_root=project_root,
            context=MigrationContext(
                api_provider="firstParty",
                user_type="ant",
                legacy_model_remap_enabled=True,
                opus1m_merge_enabled=False,
                transcript_classifier_enabled=True,
                auto_mode_enabled_state="enabled",
                is_pro_subscriber=True,
                is_team_premium_subscriber=False,
                is_max_subscriber=False,
            ),
        )
        if migration_errors:
            errors.append(f"run_all_migrations reported errors: {migration_errors}")
        reset_settings_cache()
        migrated_global = get_global_config_for_home(config_home)
        migrated_user = (
            get_settings_for_source(
                "userSettings",
                config_home=config_home,
                project_root=project_root,
            )
            or {}
        )
        migrated_local = (
            get_settings_for_source(
                "localSettings",
                config_home=config_home,
                project_root=project_root,
            )
            or {}
        )
        if migrated_global.get("remoteControlAtStartup") is not True:
            errors.append("repl bridge migration did not write remoteControlAtStartup")
        if "replBridgeEnabled" in migrated_global:
            errors.append("replBridgeEnabled was not removed after migration")
        if migrated_user.get("env", {}).get("DISABLE_AUTOUPDATER") != "1":
            errors.append(
                "autoUpdates migration did not move flag into user settings env"
            )
        if migrated_user.get("skipDangerousModePermissionPrompt") is not True:
            errors.append("bypassPermissions migration did not move acceptance flag")
        if migrated_user.get("model") != "sonnet[1m]":
            errors.append("sonnet45 to sonnet46 migration did not converge model alias")
        if migrated_local.get("enableAllProjectMcpServers") is not True:
            errors.append("project MCP migration did not populate localSettings")

    return (len(errors) == 0, tuple(errors))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--validate-persistence-migrations"]:
        print(
            "Usage: python python_src/state/validate_persistence.py --validate-persistence-migrations",
            file=sys.stderr,
        )
        return 1
    passed, errors = _load_contract()()
    print("Persistence and migrations contract")
    print(f"Passed: {passed}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("STATUS: FAILED")
        return 1
    print("STATUS: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
