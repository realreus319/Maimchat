from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import Callable


def _load_validator() -> Callable[[], tuple[bool, tuple[str, ...]]]:
    if __package__ not in (None, ""):
        from .scheduler import validate_tool_scheduler_contract

        return validate_tool_scheduler_contract

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return import_module("python_src.tools.scheduler").validate_tool_scheduler_contract


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--validate-tool-scheduler"]:
        print(
            "Usage: python python_src/tools/validate_tool_scheduler.py --validate-tool-scheduler",
            file=sys.stderr,
        )
        return 1

    passed, errors = _load_validator()()
    print("Tool scheduler contract")
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
