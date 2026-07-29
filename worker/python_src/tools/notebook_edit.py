from __future__ import annotations

import json
import os
import uuid
from collections.abc import MutableMapping
from typing import Any, Mapping

from ..utils.permissions.path_validation import expand_tilde
from .shared import FILE_UNEXPECTEDLY_MODIFIED_ERROR, ReadFileState


def notebook_edit(
    notebook_path: str,
    *,
    new_source: str,
    cell_id: str | None = None,
    cell_type: str | None = None,
    edit_mode: str = "replace",
    read_file_state: MutableMapping[str, ReadFileState] | None = None,
) -> Mapping[str, Any]:
    full_path = _expand_path(notebook_path)
    if os.path.splitext(full_path)[1].lower() != ".ipynb":
        raise ValueError("NotebookEdit only supports .ipynb files")
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"Notebook does not exist: {full_path}")

    _validate_read_state(full_path, read_file_state)

    with open(full_path, encoding="utf-8") as handle:
        notebook = json.load(handle)

    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise ValueError("Notebook is missing a valid cells array")

    normalized_mode = edit_mode.strip().lower() if edit_mode else "replace"
    if normalized_mode not in {"replace", "insert", "delete"}:
        raise ValueError(f"Invalid edit_mode: {edit_mode}")

    normalized_type = (cell_type or "").strip().lower() or None
    if normalized_type is not None and normalized_type not in {"code", "markdown", "raw"}:
        raise ValueError(f"Invalid cell_type: {cell_type}")
    if normalized_mode != "insert" and normalized_type is not None:
        raise ValueError("NotebookEdit only allows cell_type when edit_mode is insert")
    if normalized_mode in {"replace", "delete"} and not (
        isinstance(cell_id, str) and cell_id.strip()
    ):
        raise ValueError(f"NotebookEdit {normalized_mode} requires cell_id")

    target_index, target_cell = _find_cell(cells, cell_id)
    final_cell_id: str | None = None
    final_cell_type = normalized_type

    if normalized_mode == "insert":
        final_cell_type = normalized_type or "code"
        final_cell_id = cell_id.strip() if isinstance(cell_id, str) and cell_id.strip() else _new_cell_id()
        new_cell = _build_cell(final_cell_id, final_cell_type, new_source)
        insert_at = len(cells) if target_index is None else target_index + 1
        cells.insert(insert_at, new_cell)
    elif normalized_mode == "replace":
        if target_index is None or not isinstance(target_cell, Mapping):
            raise ValueError("NotebookEdit replace requires an existing target cell")
        final_cell_id = _cell_id_for(target_cell, target_index)
        final_cell_type = str(target_cell.get("cell_type", normalized_type or "code"))
        target_cell["source"] = _normalize_source(new_source)
        if final_cell_type == "code":
            target_cell["outputs"] = []
            target_cell["execution_count"] = None
    else:
        if target_index is None:
            raise ValueError("NotebookEdit delete requires an existing target cell")
        final_cell_id = _cell_id_for(cells[target_index], target_index)
        final_cell_type = str(cells[target_index].get("cell_type", normalized_type or "code"))
        del cells[target_index]

    rendered = json.dumps(notebook, ensure_ascii=False, indent=1) + "\n"
    with open(full_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())

    mtime_ms = int(os.path.getmtime(full_path) * 1000)
    if read_file_state is not None:
        read_file_state[full_path] = ReadFileState(
            file_path=full_path,
            timestamp=mtime_ms,
            content=rendered,
            offset=None,
            limit=None,
            is_partial_view=False,
        )

    return {
        "notebook_path": notebook_path,
        "cell_id": final_cell_id,
        "cell_type": final_cell_type,
        "edit_mode": normalized_mode,
        "new_source": new_source,
        "success": True,
    }


def _expand_path(file_path: str) -> str:
    expanded = expand_tilde(file_path).strip()
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return expanded


def _validate_read_state(
    full_path: str,
    read_file_state: MutableMapping[str, ReadFileState] | None,
) -> None:
    if read_file_state is None:
        raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)
    state = read_file_state.get(full_path)
    if state is None or state.is_partial_view:
        raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)
    current_mtime_ms = int(os.path.getmtime(full_path) * 1000)
    if current_mtime_ms > state.timestamp:
        raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)


def _find_cell(
    cells: list[Any],
    requested_cell_id: str | None,
) -> tuple[int | None, MutableMapping[str, Any] | None]:
    if requested_cell_id is None or not requested_cell_id.strip():
        return (len(cells) - 1, cells[-1]) if cells else (None, None)

    wanted = requested_cell_id.strip()
    for index, cell in enumerate(cells):
        if not isinstance(cell, MutableMapping):
            continue
        if _cell_id_for(cell, index) == wanted:
            return index, cell
    return None, None


def _cell_id_for(cell: Mapping[str, Any], index: int) -> str:
    raw_id = cell.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return f"cell-{index}"


def _build_cell(cell_id: str, cell_type: str, source: str) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "id": cell_id,
        "cell_type": cell_type,
        "metadata": {},
        "source": _normalize_source(source),
    }
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def _normalize_source(source: str) -> list[str]:
    if source == "":
        return []
    return [line + "\n" for line in source.splitlines()] or [source]


def _new_cell_id() -> str:
    return "cell-" + uuid.uuid4().hex[:12]


__all__ = ["notebook_edit"]
