from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _HERE:
    sys.path.pop(0)


def _load_validator():
    if __package__ not in (None, ""):
        from .client import validate_mcp_resources_contract

        return validate_mcp_resources_contract

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from importlib import import_module

    return import_module(
        "python_src.services.mcp.client"
    ).validate_mcp_resources_contract


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--validate-mcp-resources"]:
        print(
            "Usage: python python_src/services/mcp/validate_mcp_resources.py --validate-mcp-resources",
            file=sys.stderr,
        )
        return 1

    passed, errors = _load_validator()()
    print("MCP resources and tool payload contract")
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
