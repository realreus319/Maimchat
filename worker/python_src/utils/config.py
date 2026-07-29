from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from typing import Any, Callable, Dict, Mapping, Optional

from .schema_registry import (
    SchemaRegistryIssue,
    validate_global_config_payload_via_registry,
)

# Time-based microCompact configuration (mirrors TS GrowthBook timeBasedMCConfig)
TIMEBASED_MICROCOMPACT_GAP_MINUTES_DEFAULT = 60
TIMEBASED_MICROCOMPACT_KEEP_RECENT_DEFAULT = 5

# AutoCompact threshold configuration
AUTOCOMPACT_THRESHOLD_DEFAULT = 0.93

# Post-compact file recovery budget constants (aligned with TypeScript compact.ts)
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000

CLAUDE_PY_CONFIG_DIR_ENV = "CLAUDE_PY_CONFIG_DIR"
DEFAULT_CLAUDE_PY_CONFIG_DIR_NAME = ".claude_py"
DEFAULT_XDG_CLAUDE_PY_CONFIG_DIR_NAME = "claude_py"
_ENV_FILE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def get_autocompact_threshold() -> float:
    """Get the auto compact threshold ratio from env var or default."""
    raw = os.environ.get("CLAUDE_CODE_AUTOCOMPACT_THRESHOLD")
    if raw is None:
        return AUTOCOMPACT_THRESHOLD_DEFAULT
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return AUTOCOMPACT_THRESHOLD_DEFAULT
    if parsed <= 0 or parsed > 1:
        return AUTOCOMPACT_THRESHOLD_DEFAULT
    return parsed


def get_timebased_microcompact_gap_minutes() -> int:
    """Get the time gap (in minutes) required before triggering a new microcompact."""
    raw = os.environ.get("CLAUDE_CODE_TIMEBASED_MC_GAP_MINUTES")
    if raw is None:
        return TIMEBASED_MICROCOMPACT_GAP_MINUTES_DEFAULT
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return TIMEBASED_MICROCOMPACT_GAP_MINUTES_DEFAULT
    if parsed <= 0:
        return TIMEBASED_MICROCOMPACT_GAP_MINUTES_DEFAULT
    return parsed


def get_timebased_microcompact_keep_recent() -> int:
    """Get the number of recent messages to keep when doing time-based microcompact."""
    raw = os.environ.get("CLAUDE_CODE_TIMEBASED_MC_KEEP_RECENT")
    if raw is None:
        return TIMEBASED_MICROCOMPACT_KEEP_RECENT_DEFAULT
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return TIMEBASED_MICROCOMPACT_KEEP_RECENT_DEFAULT
    if parsed < 0:
        return TIMEBASED_MICROCOMPACT_KEEP_RECENT_DEFAULT
    return parsed

def get_claude_config_home() -> str:
    override = os.environ.get(CLAUDE_PY_CONFIG_DIR_ENV)
    if override:
        return override
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        return os.path.join(xdg_home, DEFAULT_XDG_CLAUDE_PY_CONFIG_DIR_NAME)
    return os.path.join(os.path.expanduser("~"), DEFAULT_CLAUDE_PY_CONFIG_DIR_NAME)


def _is_env_truthy(name: str) -> bool:
    value = os.environ.get(name)
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def should_load_claude_py_env_files() -> bool:
    if _is_env_truthy("CLAUDE_PY_SKIP_DOTENV"):
        return False
    if _is_env_truthy("CI") and not _is_env_truthy("CLAUDE_PY_LOAD_DOTENV"):
        return False
    return True


def _parse_env_file_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, sep, raw_value = stripped.partition("=")
    if not sep:
        return None
    key = key.strip()
    if not _ENV_FILE_KEY_RE.match(key):
        return None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_env_file(path: str, *, override: bool = False) -> list[str]:
    loaded: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return loaded
    for line in lines:
        parsed = _parse_env_file_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def load_claude_py_env_files(*, project_root: str | None = None) -> list[str]:
    root = project_root or os.getcwd()
    candidates = [
        os.path.join(root, ".env"),
        os.path.join(get_claude_config_home(), ".env"),
    ]
    loaded: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.abspath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        loaded.extend(load_env_file(normalized))
    return loaded


def get_global_claude_file() -> str:
    return os.path.join(get_claude_config_home(), "claude.json")


def get_anthropic_api_key() -> Optional[str]:
    value = os.environ.get("ANTHROPIC_API_KEY")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_anthropic_auth_token() -> Optional[str]:
    value = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_anthropic_base_url() -> str:
    value = os.environ.get("ANTHROPIC_BASE_URL")
    if isinstance(value, str) and value.strip():
        return value.rstrip("/")
    return "https://api.anthropic.com"


def get_model_provider() -> str:
    for name in ("MODEL_PROVIDER", "CLAUDE_CODE_MODEL_PROVIDER", "CC_MODEL_PROVIDER"):
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "auto"


def get_openai_api_key() -> Optional[str]:
    names = (
        ("DEEPSEEK_API_KEY", "OPENAI_API_KEY")
        if get_model_provider() == "deepseek"
        else ("OPENAI_API_KEY", "DEEPSEEK_API_KEY")
    )
    for name in names:
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def get_openai_base_url() -> str:
    value = os.environ.get("OPENAI_BASE_URL")
    if isinstance(value, str) and value.strip():
        return value.rstrip("/")
    value = os.environ.get("DEEPSEEK_BASE_URL")
    if isinstance(value, str) and value.strip():
        return value.rstrip("/")
    provider = get_model_provider()
    if provider == "deepseek" or (
        os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY")
    ):
        return "https://api.deepseek.com"
    return "https://api.openai.com"


def get_openai_default_model() -> Optional[str]:
    for name in (
        "OPENAI_MODEL",
        "OPENAI_DEFAULT_MODEL",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_DEFAULT_MODEL",
    ):
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    provider = get_model_provider()
    if provider == "deepseek" or (
        os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY")
    ):
        return "deepseek-v4-pro"
    if provider in {"openai", "openai-chat", "openai-compatible"}:
        return "gpt-4o-mini"
    return None


def get_openai_system_role() -> str:
    value = os.environ.get("OPENAI_SYSTEM_ROLE")
    if isinstance(value, str) and value.strip().lower() in {"system", "developer"}:
        return value.strip().lower()
    return "system"


def get_api_timeout_ms(default: int = 600000) -> int:
    raw = os.environ.get("API_TIMEOUT_MS")
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def get_global_claude_file_for_home(config_home: str) -> str:
    return os.path.join(config_home, "claude.json")


_GLOBAL_CONFIG_DEFAULTS: Dict[str, Any] = {
    "numStartups": 0,
    "theme": "dark",
    "preferredNotifChannel": "auto",
    "verbose": False,
    "autoCompactEnabled": True,
    "showTurnDuration": True,
    "env": {},
    "tipsHistory": {},
    "memoryUsageCount": 0,
    "promptQueueUseCount": 0,
    "todoFeatureEnabled": True,
    "showExpandedTodos": False,
    "messageIdleNotifThresholdMs": 60000,
    "autoConnectIde": False,
    "autoInstallIdeExtension": True,
    "fileCheckpointingEnabled": True,
    "terminalProgressBarEnabled": True,
    "cachedStatsigGates": {},
    "respectGitignore": True,
    "copyFullResponse": False,
}


def create_default_global_config() -> Dict[str, Any]:
    return deepcopy(_GLOBAL_CONFIG_DEFAULTS)


_config_cache: Optional[Dict[str, Any]] = None


def get_global_config() -> Dict[str, Any]:
    return get_global_config_for_home(None)


def get_global_config_for_home(config_home: Optional[str]) -> Dict[str, Any]:
    global _config_cache
    if config_home is None and _config_cache is not None:
        return _config_cache

    path = (
        get_global_claude_file()
        if config_home is None
        else get_global_claude_file_for_home(config_home)
    )
    loaded = _read_global_config(path)
    if config_home is None:
        _config_cache = loaded
    return loaded


def _read_global_config(path: str) -> Dict[str, Any]:
    loaded, _ = _read_global_config_with_errors(path)
    return loaded


def _read_global_config_with_errors(path: str) -> tuple[Dict[str, Any], tuple[str, ...]]:
    defaults = create_default_global_config()
    try:
        with open(path) as f:
            content = f.read()
    except (FileNotFoundError, OSError):
        return defaults, ()

    if content.strip() == "":
        return defaults, ()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return defaults, ()

    if not isinstance(data, dict):
        return defaults, ()

    result = validate_global_config_payload_via_registry(
        data,
        defaults=defaults,
        file_path=path,
    )
    return result.value or defaults, _format_global_config_issues(result.issues)


def reset_global_config_cache() -> None:
    global _config_cache
    _config_cache = None


def save_global_config(
    updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    *,
    config_home: Optional[str] = None,
) -> Dict[str, Any]:
    path = (
        get_global_claude_file()
        if config_home is None
        else get_global_claude_file_for_home(config_home)
    )
    current = _read_global_config(path)
    updated = updater(deepcopy(current))
    if not isinstance(updated, Mapping):
        raise ValueError("Global config updater must return a JSON object")
    result = validate_global_config_payload_via_registry(
        dict(updated),
        defaults=create_default_global_config(),
        file_path=path,
    )
    validated = result.value or create_default_global_config()
    issues = result.issues
    if issues:
        raise ValueError(_format_global_config_write_error(path, issues))
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=parent,
        prefix=".claude_py.json.tmp.",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(validated, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if config_home is None:
        global _config_cache
        _config_cache = deepcopy(validated)
    return validated


def _format_global_config_issues(
    issues: tuple[SchemaRegistryIssue, ...],
) -> tuple[str, ...]:
    return tuple(f"{issue.path}: {issue.message}" for issue in issues)


def _format_global_config_write_error(
    path: str,
    issues: tuple[SchemaRegistryIssue, ...],
) -> str:
    details = ", ".join(
        f"{issue.path}: {issue.message}"
        for issue in issues[:3]
    )
    suffix = "" if len(issues) <= 3 else f" (+{len(issues) - 3} more)"
    return f"Refusing to save invalid global config for {path}: {details}{suffix}"
