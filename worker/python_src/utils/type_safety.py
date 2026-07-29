"""Runtime type-safety helpers mirroring TS exhaustive checks."""

from __future__ import annotations

from dataclasses import is_dataclass
from types import MappingProxyType
from typing import Any, NoReturn


def assert_never(value: NoReturn, *, message: str | None = None) -> NoReturn:
    detail = message or f"Unhandled value: {value!r}"
    raise AssertionError(detail)


def deep_freeze(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, dict):
        return MappingProxyType({key: deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(deep_freeze(item) for item in value)
    return value


def is_deep_immutable(value: Any) -> bool:
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return True
    if isinstance(value, MappingProxyType):
        return all(is_deep_immutable(item) for item in value.values())
    if isinstance(value, tuple):
        return all(is_deep_immutable(item) for item in value)
    if isinstance(value, frozenset):
        return all(is_deep_immutable(item) for item in value)
    if is_dataclass(value):
        frozen = getattr(type(value), "__dataclass_params__", None)
        if not getattr(frozen, "frozen", False):
            return False
        return all(is_deep_immutable(getattr(value, field)) for field in value.__dataclass_fields__)
    return False


__all__ = ["assert_never", "deep_freeze", "is_deep_immutable"]
