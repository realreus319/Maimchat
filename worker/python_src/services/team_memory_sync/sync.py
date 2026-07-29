from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable, Mapping

from ...memdir.team_mem_paths import get_team_mem_path
from .secret_scanner import scan_for_secrets

DEFAULT_MAX_BATCH_BYTES = 200_000
DEFAULT_MAX_FILE_SIZE_BYTES = 250_000


@dataclass(frozen=True)
class SkippedSecretFile:
    path: str
    rule_id: str
    label: str


@dataclass
class TeamMemorySyncState:
    shared_checksums: dict[str, str] = field(default_factory=dict)
    local_checksums: dict[str, str] = field(default_factory=dict)
    last_sync_at: float | None = None


@dataclass(frozen=True)
class TeamMemoryPullResult:
    success: bool
    files_written: int
    entry_count: int
    skipped_secrets: tuple[SkippedSecretFile, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class TeamMemoryPushResult:
    success: bool
    files_uploaded: int
    skipped_secrets: tuple[SkippedSecretFile, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class TeamMemorySyncResult:
    success: bool
    files_pulled: int
    files_pushed: int
    pull_skipped_secrets: tuple[SkippedSecretFile, ...] = ()
    push_skipped_secrets: tuple[SkippedSecretFile, ...] = ()
    error: str | None = None


def create_sync_state() -> TeamMemorySyncState:
    return TeamMemorySyncState()


def hash_content(content: str) -> str:
    return "sha256:" + sha256(content.encode("utf-8")).hexdigest()


def batch_delta_by_bytes(
    delta: Mapping[str, str],
    *,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
) -> tuple[dict[str, str], ...]:
    keys = sorted(delta)
    if not keys:
        return ()

    empty_body_bytes = len('{"entries":{}}'.encode("utf-8"))

    def entry_bytes(key: str, value: str) -> int:
        return (
            len(json.dumps(key, ensure_ascii=False).encode("utf-8"))
            + len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            + 2
        )

    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_bytes = empty_body_bytes

    for key in keys:
        value = delta[key]
        added = entry_bytes(key, value)
        if current and current_bytes + added > max_batch_bytes:
            batches.append(current)
            current = {}
            current_bytes = empty_body_bytes
        current[key] = value
        current_bytes += added

    batches.append(current)
    return tuple(batches)


def pull_team_memory(
    state: TeamMemorySyncState,
    shared_dir: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> TeamMemoryPullResult:
    try:
        local_dir = _team_mem_dir(project_root=project_root, config_home=config_home)
        shared_entries, skipped_secrets = _read_team_memory_entries(
            shared_dir,
            skip_secrets=True,
        )
        local_entries, _ = _read_team_memory_entries(local_dir, skip_secrets=False)
        shared_hashes = _hash_entries(shared_entries)
        local_hashes = _hash_entries(local_entries)
        delta = {
            path: content
            for path, content in shared_entries.items()
            if local_hashes.get(path) != shared_hashes[path]
        }
        files_written = _write_team_memory_entries(local_dir, delta)
        _refresh_state(
            state,
            shared_hashes=shared_hashes,
            local_hashes={**local_hashes, **{key: shared_hashes[key] for key in delta}},
        )
        return TeamMemoryPullResult(
            success=True,
            files_written=files_written,
            entry_count=len(shared_entries),
            skipped_secrets=skipped_secrets,
        )
    except OSError as exc:
        return TeamMemoryPullResult(
            success=False,
            files_written=0,
            entry_count=0,
            error=str(exc),
        )


def push_team_memory(
    state: TeamMemorySyncState,
    shared_dir: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> TeamMemoryPushResult:
    try:
        local_dir = _team_mem_dir(project_root=project_root, config_home=config_home)
        local_entries, skipped_secrets = _read_team_memory_entries(
            local_dir,
            skip_secrets=True,
        )
        shared_entries, _ = _read_team_memory_entries(shared_dir, skip_secrets=False)
        local_hashes = _hash_entries(local_entries)
        shared_hashes = _hash_entries(shared_entries)
        delta = {
            path: content
            for path, content in local_entries.items()
            if shared_hashes.get(path) != local_hashes[path]
        }
        files_uploaded = 0
        for batch in batch_delta_by_bytes(delta):
            files_uploaded += _write_team_memory_entries(shared_dir, batch)
        _refresh_state(
            state,
            shared_hashes={**shared_hashes, **{key: local_hashes[key] for key in delta}},
            local_hashes=local_hashes,
        )
        return TeamMemoryPushResult(
            success=True,
            files_uploaded=files_uploaded,
            skipped_secrets=skipped_secrets,
        )
    except OSError as exc:
        return TeamMemoryPushResult(
            success=False,
            files_uploaded=0,
            error=str(exc),
        )


def sync_team_memory(
    state: TeamMemorySyncState,
    shared_dir: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> TeamMemorySyncResult:
    pull_result = pull_team_memory(
        state,
        shared_dir,
        project_root=project_root,
        config_home=config_home,
    )
    if not pull_result.success:
        return TeamMemorySyncResult(
            success=False,
            files_pulled=0,
            files_pushed=0,
            error=pull_result.error,
        )

    push_result = push_team_memory(
        state,
        shared_dir,
        project_root=project_root,
        config_home=config_home,
    )
    if not push_result.success:
        return TeamMemorySyncResult(
            success=False,
            files_pulled=pull_result.files_written,
            files_pushed=0,
            pull_skipped_secrets=pull_result.skipped_secrets,
            error=push_result.error,
        )

    return TeamMemorySyncResult(
        success=True,
        files_pulled=pull_result.files_written,
        files_pushed=push_result.files_uploaded,
        pull_skipped_secrets=pull_result.skipped_secrets,
        push_skipped_secrets=push_result.skipped_secrets,
    )


@dataclass
class TeamMemoryWatcher:
    state: TeamMemorySyncState
    shared_dir: str
    project_root: str | None = None
    config_home: str | None = None
    poll_interval_seconds: float = 1.0
    on_error: Callable[[Exception], None] | None = None
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _wake_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _last_local_fingerprint: tuple[str, ...] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_shared_fingerprint: tuple[str, ...] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def start(self) -> "TeamMemoryWatcher":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._last_local_fingerprint = _directory_fingerprint(
            _team_mem_dir(
                project_root=self.project_root,
                config_home=self.config_home,
            )
        )
        self._last_shared_fingerprint = _directory_fingerprint(self.shared_dir)
        self._thread = threading.Thread(
            target=self._run,
            name="team-memory-sync",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            woke = self._wake_event.wait(self.poll_interval_seconds)
            if self._stop_event.is_set():
                return
            self._wake_event.clear()
            try:
                self._sync_once(force_push=woke)
            except Exception as exc:  # pragma: no cover - defensive thread guard
                if self.on_error is not None:
                    self.on_error(exc)

    def _sync_once(self, *, force_push: bool) -> None:
        local_dir = _team_mem_dir(
            project_root=self.project_root,
            config_home=self.config_home,
        )
        current_local = _directory_fingerprint(local_dir)
        current_shared = _directory_fingerprint(self.shared_dir)
        local_changed = current_local != self._last_local_fingerprint
        shared_changed = current_shared != self._last_shared_fingerprint

        if force_push and not shared_changed:
            push_team_memory(
                self.state,
                self.shared_dir,
                project_root=self.project_root,
                config_home=self.config_home,
            )
        elif local_changed and shared_changed:
            sync_team_memory(
                self.state,
                self.shared_dir,
                project_root=self.project_root,
                config_home=self.config_home,
            )
        elif local_changed:
            push_team_memory(
                self.state,
                self.shared_dir,
                project_root=self.project_root,
                config_home=self.config_home,
            )
        elif shared_changed:
            pull_team_memory(
                self.state,
                self.shared_dir,
                project_root=self.project_root,
                config_home=self.config_home,
            )

        self._last_local_fingerprint = _directory_fingerprint(local_dir)
        self._last_shared_fingerprint = _directory_fingerprint(self.shared_dir)


def start_team_memory_watcher(
    state: TeamMemorySyncState,
    shared_dir: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
    poll_interval_seconds: float = 1.0,
    on_error: Callable[[Exception], None] | None = None,
) -> TeamMemoryWatcher:
    watcher = TeamMemoryWatcher(
        state=state,
        shared_dir=shared_dir,
        project_root=project_root,
        config_home=config_home,
        poll_interval_seconds=poll_interval_seconds,
        on_error=on_error,
    )
    return watcher.start()


def notify_team_memory_write(watcher: TeamMemoryWatcher) -> None:
    watcher.wake()


def _team_mem_dir(
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> str:
    return os.path.abspath(
        get_team_mem_path(project_root=project_root, config_home=config_home)
    )


def _hash_entries(entries: Mapping[str, str]) -> dict[str, str]:
    return {path: hash_content(content) for path, content in entries.items()}


def _refresh_state(
    state: TeamMemorySyncState,
    *,
    shared_hashes: Mapping[str, str],
    local_hashes: Mapping[str, str],
) -> None:
    state.shared_checksums.clear()
    state.shared_checksums.update(shared_hashes)
    state.local_checksums.clear()
    state.local_checksums.update(local_hashes)
    state.last_sync_at = time.time()


def _read_team_memory_entries(
    base_dir: str,
    *,
    skip_secrets: bool,
) -> tuple[dict[str, str], tuple[SkippedSecretFile, ...]]:
    root = os.path.abspath(base_dir)
    if not os.path.exists(root):
        return {}, ()

    entries: dict[str, str] = {}
    skipped: list[SkippedSecretFile] = []

    for current_root, _, filenames in os.walk(root):
        filenames.sort()
        for filename in filenames:
            full_path = os.path.join(current_root, filename)
            rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")
            _safe_join(root, rel_path)
            try:
                size_bytes = os.path.getsize(full_path)
            except OSError:
                continue
            if size_bytes > DEFAULT_MAX_FILE_SIZE_BYTES:
                continue
            try:
                with open(full_path, encoding="utf-8") as handle:
                    content = handle.read()
            except (OSError, UnicodeDecodeError):
                continue

            if skip_secrets:
                matches = scan_for_secrets(content)
                if matches:
                    first = matches[0]
                    skipped.append(
                        SkippedSecretFile(
                            path=rel_path,
                            rule_id=first.rule_id,
                            label=first.label,
                        )
                    )
                    continue

            entries[rel_path] = content

    return entries, tuple(skipped)


def _write_team_memory_entries(base_dir: str, entries: Mapping[str, str]) -> int:
    written = 0
    root = os.path.abspath(base_dir)
    for rel_path, content in entries.items():
        destination = _safe_join(root, rel_path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            with open(destination, encoding="utf-8") as handle:
                if handle.read() == content:
                    continue
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            pass
        _atomic_write(destination, content)
        written += 1
    return written


def _atomic_write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        dir=parent,
        prefix=".team-memory.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _safe_join(base_dir: str, rel_path: str) -> str:
    normalized = rel_path.replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        raise ValueError(f"Unsafe team memory path: {rel_path!r}")
    candidate = os.path.abspath(os.path.join(base_dir, normalized))
    base = os.path.abspath(base_dir)
    if os.path.commonpath((base, candidate)) != base:
        raise ValueError(f"Unsafe team memory path: {rel_path!r}")
    return candidate


def _directory_fingerprint(base_dir: str) -> tuple[str, ...]:
    root = os.path.abspath(base_dir)
    if not os.path.exists(root):
        return ()

    fingerprint: list[str] = []
    for current_root, _, filenames in os.walk(root):
        filenames.sort()
        for filename in filenames:
            full_path = os.path.join(current_root, filename)
            rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")
            try:
                stat_result = os.stat(full_path)
            except OSError:
                continue
            fingerprint.append(
                f"{rel_path}:{stat_result.st_size}:{stat_result.st_mtime_ns}"
            )
    fingerprint.sort()
    return tuple(fingerprint)
