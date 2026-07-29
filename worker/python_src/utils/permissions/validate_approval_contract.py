from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import Callable


def _load_validator() -> Callable[[], tuple[bool, tuple[str, ...]]]:
    if __package__ not in (None, ""):
        from .approval import validate_approval_contract

        return validate_approval_contract

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return import_module(
        "python_src.utils.permissions.approval"
    ).validate_approval_contract


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--validate-approval-contract"]:
        print(
            "Usage: python python_src/utils/permissions/validate_approval_contract.py --validate-approval-contract",
            file=sys.stderr,
        )
        return 1

    passed, errors = _load_validator()()
    print("Approval contract")
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
