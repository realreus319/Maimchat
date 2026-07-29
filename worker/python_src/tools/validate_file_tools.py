from __future__ import annotations

import os
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


def _load_validator() -> Callable[[], Tuple[bool, Tuple[str, ...]]]:
    if __package__ in (None, ""):
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(here))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        mod = import_module("python_src.tools.validate_file_tools")
        return mod.validate_file_tools_contract

    def _run() -> Tuple[bool, Tuple[str, ...]]:
        errors: List[str] = []
        _read_tests(errors)
        _write_tests(errors)
        _edit_tests(errors)
        _validation_tests(errors)
        _permission_tests(errors)
        if errors:
            for error in errors:
                print("ERROR: {}".format(error))
            print("STATUS: FAILED ({} error(s))".format(len(errors)))
            return False, tuple(errors)
        print("STATUS: PASSED")
        return True, ()

    return _run


def _read_tests(errors: List[str]) -> None:
    from .file_read import file_read
    from .shared import ReadFileState, ReadResult

    state: Dict[str, ReadFileState] = {}
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "read.txt")
        Path(path).write_text("hello\nworld\n", encoding="utf-8")
        result = file_read(path, read_file_state=state)
        if not isinstance(result, ReadResult):
            errors.append("read: must return ReadResult")
            return
        if result.content != "hello\nworld":
            errors.append("read: content mismatch {!r}".format(result.content))
        if result.total_lines != 2:
            errors.append("read: total_lines mismatch {}".format(result.total_lines))

        partial = file_read(path, offset=2, limit=1, read_file_state=state)
        if partial.content != "world":
            errors.append("read: offset/limit mismatch {!r}".format(partial.content))


def _write_tests(errors: List[str]) -> None:
    from .file_read import file_read
    from .file_write import file_write
    from .shared import ReadFileState, WriteResult

    with tempfile.TemporaryDirectory() as td:
        state: Dict[str, ReadFileState] = {}
        created = os.path.join(td, "created.txt")
        result = file_write(created, "alpha\n", read_file_state=state)
        if not isinstance(result, WriteResult):
            errors.append("write: must return WriteResult")
            return
        if result.result_type != "create":
            errors.append("write: create expected, got {}".format(result.result_type))
        if Path(created).read_text(encoding="utf-8") != "alpha\n":
            errors.append("write: create content mismatch")

        target = os.path.join(td, "existing.txt")
        Path(target).write_text("before\n", encoding="utf-8")
        file_read(target, read_file_state=state)
        updated = file_write(target, "after\n", read_file_state=state)
        if updated.result_type != "update":
            errors.append("write: update expected, got {}".format(updated.result_type))
        if updated.original_file != "before\n":
            errors.append(
                "write: original_file mismatch {!r}".format(updated.original_file)
            )


def _edit_tests(errors: List[str]) -> None:
    from .file_read import file_read
    from .file_edit import file_edit
    from .shared import EditResult, ReadFileState

    with tempfile.TemporaryDirectory() as td:
        state: Dict[str, ReadFileState] = {}
        path = os.path.join(td, "edit.txt")
        Path(path).write_text("hello\nhello\n", encoding="utf-8")
        file_read(path, read_file_state=state)
        result = file_edit(
            path,
            old_string="hello",
            new_string="HELLO",
            replace_all=False,
            read_file_state=state,
        )
        if not isinstance(result, EditResult):
            errors.append("edit: must return EditResult")
            return
        if result.old_string != "hello":
            errors.append("edit: old_string mismatch {!r}".format(result.old_string))
        if Path(path).read_text(encoding="utf-8") != "HELLO\nhello\n":
            errors.append("edit: single replacement mismatch")

        created = os.path.join(td, "new.txt")
        created_result = file_edit(
            created,
            old_string="",
            new_string="fresh\n",
            read_file_state=state,
        )
        if created_result.original_file is not None:
            errors.append("edit: new file original_file must be None")


def _validation_tests(errors: List[str]) -> None:
    from .file_edit import validate_edit_input
    from .file_read import validate_read_input
    from .file_write import validate_write_input
    from ..types.permissions import ToolPermissionContext

    ctx = ToolPermissionContext()

    blocked = validate_read_input("/dev/zero", permission_context=ctx)
    if blocked.result:
        errors.append("validate_read: /dev/zero must be rejected")

    binary = validate_read_input("test.exe", permission_context=ctx)
    if binary.result:
        errors.append("validate_read: .exe must be rejected")

    same = validate_edit_input(
        "test.txt",
        old_string="a",
        new_string="a",
        permission_context=ctx,
    )
    if same.result or same.error_code != 1:
        errors.append("validate_edit: same-string rejection mismatch")

    unc = validate_edit_input(
        "\\\\server\\share",
        old_string="a",
        new_string="b",
        permission_context=ctx,
    )
    if not unc.result:
        errors.append("validate_edit: UNC paths must defer to permission check")

    with tempfile.TemporaryDirectory() as td:
        from .shared import ReadFileState

        state: Dict[str, ReadFileState] = {}
        path = os.path.join(td, "write.txt")
        Path(path).write_text("before\n", encoding="utf-8")
        unread = validate_write_input(
            path,
            content="after\n",
            permission_context=ctx,
            read_file_state=state,
        )
        if unread.result or unread.error_code != 2:
            errors.append("validate_write: unread existing file must be rejected")


def _permission_tests(errors: List[str]) -> None:
    from .file_edit import check_edit_permission
    from .file_read import check_read_permission
    from .file_write import check_write_permission
    from ..types.permissions import (
        PermissionAllowDecision,
        PermissionAskDecision,
        PermissionDenyDecision,
        ToolPermissionContext,
    )

    ctx = ToolPermissionContext()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "perm.txt")
        Path(path).write_text("value", encoding="utf-8")
        for name, decision in [
            ("read", check_read_permission(path, ctx)),
            ("write", check_write_permission(path, ctx)),
            ("edit", check_edit_permission(path, ctx)),
        ]:
            if not isinstance(
                decision,
                (
                    PermissionAllowDecision,
                    PermissionAskDecision,
                    PermissionDenyDecision,
                ),
            ):
                errors.append("perm_{}: invalid decision type".format(name))


def validate_file_tools_contract() -> Tuple[bool, Tuple[str, ...]]:
    runner = _load_validator()
    return runner()


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["--validate-file-tools"]:
        print(
            "Usage: python python_src/tools/validate_file_tools.py --validate-file-tools",
            file=sys.stderr,
        )
        return 1

    passed, errors = validate_file_tools_contract()
    print("File tools contract")
    print("Passed: {}".format(passed))
    if errors:
        for error in errors:
            print("ERROR: {}".format(error))
        print("STATUS: FAILED")
        return 1
    print("STATUS: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
