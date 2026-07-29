import os
import sys

if __name__ == "__main__" and __package__ in (None, ""):
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
    sys.path.insert(0, _REPO_ROOT)

# pyright: reportMissingImports=false
from python_src.tui_interaction import validate_tui_interaction_contract


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--validate-tui-interaction"]:
        print(
            "Usage: python -m python_src.validate_tui_interaction --validate-tui-interaction",
            file=sys.stderr,
        )
        return 1

    passed, errors = validate_tui_interaction_contract()
    print("TUI interaction contract")
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
