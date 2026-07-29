from __future__ import annotations

from typing import Callable, List, Sequence, Tuple

from ._shared import MigrationContext, default_context
from .migrate_auto_updates_to_settings import migrate_auto_updates_to_settings
from .migrate_bypass_permissions_accepted_to_settings import (
    migrate_bypass_permissions_accepted_to_settings,
)
from .migrate_enable_all_project_mcp_servers_to_settings import (
    migrate_enable_all_project_mcp_servers_to_settings,
)
from .migrate_fennec_to_opus import migrate_fennec_to_opus
from .migrate_legacy_opus_to_current import migrate_legacy_opus_to_current
from .migrate_opus_to_opus1m import migrate_opus_to_opus1m
from .migrate_repl_bridge_enabled_to_remote_control_at_startup import (
    migrate_repl_bridge_enabled_to_remote_control_at_startup,
)
from .migrate_sonnet1m_to_sonnet45 import migrate_sonnet1m_to_sonnet45
from .migrate_sonnet45_to_sonnet46 import migrate_sonnet45_to_sonnet46
from .reset_auto_mode_opt_in_for_default_offer import (
    reset_auto_mode_opt_in_for_default_offer,
)
from .reset_pro_to_opus_default import reset_pro_to_opus_default

MigrationFunc = Callable[..., bool]

MIGRATIONS: Sequence[Tuple[str, MigrationFunc]] = (
    ("migrateAutoUpdatesToSettings", migrate_auto_updates_to_settings),
    (
        "migrateBypassPermissionsAcceptedToSettings",
        migrate_bypass_permissions_accepted_to_settings,
    ),
    (
        "migrateEnableAllProjectMcpServersToSettings",
        migrate_enable_all_project_mcp_servers_to_settings,
    ),
    ("migrateFennecToOpus", migrate_fennec_to_opus),
    ("migrateLegacyOpusToCurrent", migrate_legacy_opus_to_current),
    ("migrateOpusToOpus1m", migrate_opus_to_opus1m),
    (
        "migrateReplBridgeEnabledToRemoteControlAtStartup",
        migrate_repl_bridge_enabled_to_remote_control_at_startup,
    ),
    ("migrateSonnet1mToSonnet45", migrate_sonnet1m_to_sonnet45),
    ("migrateSonnet45ToSonnet46", migrate_sonnet45_to_sonnet46),
    (
        "resetAutoModeOptInForDefaultOffer",
        reset_auto_mode_opt_in_for_default_offer,
    ),
    ("resetProToOpusDefault", reset_pro_to_opus_default),
)


def run_all_migrations(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> List[str]:
    migration_context = context or default_context()
    errors: List[str] = []
    for name, func in MIGRATIONS:
        try:
            func(
                config_home=config_home,
                project_root=project_root,
                context=migration_context,
            )
        except TypeError:
            try:
                func(config_home=config_home, project_root=project_root)
            except TypeError:
                func(config_home=config_home)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return errors


__all__ = [
    "MigrationContext",
    "default_context",
    "migrate_auto_updates_to_settings",
    "migrate_bypass_permissions_accepted_to_settings",
    "migrate_enable_all_project_mcp_servers_to_settings",
    "migrate_fennec_to_opus",
    "migrate_legacy_opus_to_current",
    "migrate_opus_to_opus1m",
    "migrate_repl_bridge_enabled_to_remote_control_at_startup",
    "migrate_sonnet1m_to_sonnet45",
    "migrate_sonnet45_to_sonnet46",
    "reset_auto_mode_opt_in_for_default_offer",
    "reset_pro_to_opus_default",
    "run_all_migrations",
]
