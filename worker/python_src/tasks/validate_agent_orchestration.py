from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import Callable, Tuple


def _load_validator() -> Callable[[], Tuple[bool, Tuple[str, ...]]]:
    if __package__ not in (None, ""):
        from .agent_orchestration import validate_agent_orchestration_contract

        return validate_agent_orchestration_contract

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return import_module(
        "python_src.tasks.agent_orchestration"
    ).validate_agent_orchestration_contract


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--validate-agent-orchestration"]:
        print(
            "Usage: python python_src/tasks/validate_agent_orchestration.py --validate-agent-orchestration",
            file=sys.stderr,
        )
        return 1

    passed, errors = _load_validator()()
    print("Agent orchestration contract")
    print("Passed: {}".format(passed))
    if errors:
        for error in errors:
            print("ERROR: {}".format(error))
        print("STATUS: FAILED")
        return 1
    print("STATUS: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
