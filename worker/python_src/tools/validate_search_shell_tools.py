from __future__ import annotations

import os
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Callable, Optional


def _load_validator() -> Callable[[], bool]:
    if __package__ in (None, ""):
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(here))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        mod = import_module("python_src.tools.validate_search_shell_tools")
        return mod.validate_search_and_shell_tools_contract

    def _run() -> bool:
        errors: list = []
        _glob_tests(errors)
        _grep_tests(errors)
        _bash_tests(errors)
        if errors:
            for error in errors:
                print("ERROR: {}".format(error))
            print("STATUS: FAILED ({} error(s))".format(len(errors)))
            return False
        print("STATUS: PASSED")
        return True

    return _run


def _glob_tests(errors: list) -> None:
    from .glob_tool import GlobInput, GlobOutput, GlobTool

    with tempfile.TemporaryDirectory() as td:
        Path(td, "a.py").write_text("pass")
        Path(td, "b.txt").write_text("hello")
        inp = GlobInput(pattern="*.py", path=td)
        out = GlobTool.call(inp)
        if not isinstance(out, GlobOutput):
            errors.append("glob: call must return GlobOutput")
            return
        if out.num_files != 1:
            errors.append("glob: expected 1 file, got {}".format(out.num_files))
        if out.truncated:
            errors.append("glob: single file should not be truncated")
        if not out.filenames:
            errors.append("glob: filenames must not be empty for matching pattern")
        elif "a.py" not in out.filenames[0]:
            errors.append(
                "glob: expected a.py in filenames, got {}".format(out.filenames)
            )
        if out.duration_ms <= 0:
            errors.append("glob: duration_ms must be positive")

    empty_out = GlobOutput(filenames=[], duration_ms=1.0, num_files=0, truncated=False)
    if GlobTool.map_result(empty_out) != "No files found":
        errors.append("glob: map_result for empty must return 'No files found'")

    trunc_out = GlobOutput(
        filenames=["f1.py"], duration_ms=1.0, num_files=1, truncated=True
    )
    result = GlobTool.map_result(trunc_out)
    if "truncated" not in result.lower():
        errors.append("glob: map_result for truncated must mention truncation")

    if not GlobTool.is_concurrency_safe({"pattern": "*.py"}):
        errors.append("glob: must be concurrency-safe")
    if not GlobTool.is_read_only():
        errors.append("glob: must be read-only")

    ok, _ = GlobTool.validate_input({})
    if ok:
        errors.append("glob: validate_input must reject empty pattern")
    ok, _ = GlobTool.validate_input({"pattern": "*.py"})
    if not ok:
        errors.append("glob: validate_input must accept valid pattern")
    ok, _ = GlobTool.validate_input({"pattern": "*.py", "path": "/nonexistent/xyz"})
    if ok:
        errors.append("glob: validate_input must reject nonexistent path")


def _grep_tests(errors: list) -> None:
    from .grep_tool import GrepInput, GrepOutput, GrepTool

    with tempfile.TemporaryDirectory() as td:
        Path(td, "a.py").write_text("def hello():\n    pass\n")
        Path(td, "b.txt").write_text("world\nhello\n")
        os.makedirs(os.path.join(td, ".git"), exist_ok=True)
        Path(td, ".git", "config").write_text("[core]\n")

        inp = GrepInput(pattern="hello", path=td, output_mode="files_with_matches")
        out = GrepTool.call(inp)
        if not isinstance(out, GrepOutput):
            errors.append("grep: call must return GrepOutput")
            return
        if out.mode != "files_with_matches":
            errors.append(
                "grep: expected mode=files_with_matches, got {}".format(out.mode)
            )
        if (out.num_files or 0) < 2:
            errors.append(
                "grep: expected at least 2 files with 'hello', got {}".format(
                    out.num_files
                )
            )

        git_matches = [f for f in out.filenames if ".git" in f]
        if git_matches:
            errors.append(
                "grep: must exclude .git directory, got: {}".format(git_matches)
            )

        inp_content = GrepInput(pattern="hello", path=td, output_mode="content")
        out_content = GrepTool.call(inp_content)
        if not out_content.content:
            errors.append("grep: content mode must produce content")
        if out_content.num_lines is not None and out_content.num_lines <= 0:
            errors.append("grep: content mode must have positive num_lines")

        inp_count = GrepInput(pattern="hello", path=td, output_mode="count")
        out_count = GrepTool.call(inp_count)
        if out_count.num_files is not None and out_count.num_files < 2:
            errors.append("grep: count mode must find at least 2 files")
        if out_count.num_matches is not None and out_count.num_matches < 2:
            errors.append("grep: count mode must find at least 2 matches")

    empty_out = GrepOutput(filenames=[], mode="files_with_matches", num_files=0)
    if GrepTool.map_result(empty_out) != "No files found":
        errors.append(
            "grep: map_result empty files_with_matches must say 'No files found'"
        )

    empty_content = GrepOutput(filenames=[], mode="content", num_files=0, content="")
    result = GrepTool.map_result(empty_content)
    if "No matches found" not in result:
        errors.append("grep: map_result empty content must say 'No matches found'")

    if not GrepTool.is_concurrency_safe({"pattern": "test"}):
        errors.append("grep: must be concurrency-safe")
    if not GrepTool.is_read_only():
        errors.append("grep: must be read-only")

    ok, _ = GrepTool.validate_input({})
    if ok:
        errors.append("grep: validate_input must reject empty pattern")
    ok, _ = GrepTool.validate_input({"pattern": "test"})
    if not ok:
        errors.append("grep: validate_input must accept valid pattern")


def _bash_tests(errors: list) -> None:
    from .bash_tool import (
        BASH_TOOL_NAME,
        BashInput,
        BashOutput,
        BashTool,
        ShellError,
        is_search_or_read_bash_command,
        is_silent_bash_command,
    )
    from .glob_tool import GLOB_TOOL_NAME
    from .grep_tool import GREP_TOOL_NAME

    out = BashTool.call(BashInput(command="echo hello"))
    if not isinstance(out, BashOutput):
        errors.append("bash: call must return BashOutput")
        return
    if out.exit_code != 0:
        errors.append("bash: echo must succeed, got exit_code={}".format(out.exit_code))
    if "hello" not in out.stdout:
        errors.append(
            "bash: echo output must contain 'hello', got: {}".format(out.stdout)
        )
    if out.timed_out:
        errors.append("bash: echo must not time out")
    if out.interrupted:
        errors.append("bash: echo must not be interrupted")
    if out.duration_ms <= 0:
        errors.append("bash: duration_ms must be positive")

    out_fail = BashTool.call(BashInput(command="false"))
    if out_fail.exit_code == 0:
        errors.append("bash: 'false' must return non-zero exit code")

    out_timeout = BashTool.call(BashInput(command="sleep 60", timeout=500))
    if not out_timeout.timed_out:
        errors.append("bash: long sleep with short timeout must time out")

    for cmd, expected in [
        ("ls", {"isSearch": False, "isRead": False, "isList": True}),
        ("cat file.txt", {"isSearch": False, "isRead": True, "isList": False}),
        ("grep pattern", {"isSearch": True, "isRead": False, "isList": False}),
        ("find . -name '*.py'", {"isSearch": True, "isRead": False, "isList": False}),
        ("rm file.txt", {"isSearch": False, "isRead": False, "isList": False}),
        ("echo hello", {"isSearch": False, "isRead": False, "isList": False}),
        (
            "ls dir && echo --- && ls dir2",
            {"isSearch": False, "isRead": False, "isList": True},
        ),
    ]:
        result = is_search_or_read_bash_command(cmd)
        for key in expected:
            if result.get(key) != expected[key]:
                errors.append(
                    "bash: isSearchOrReadBashCommand('{}') expected {}={}, got {}".format(
                        cmd, key, expected[key], result.get(key)
                    )
                )

    if not BashTool.is_read_only({"command": "ls"}):
        errors.append("bash: 'ls' must be classified read-only")
    if not BashTool.is_read_only({"command": "cat file.txt"}):
        errors.append("bash: 'cat file.txt' must be classified read-only")
    if BashTool.is_read_only({"command": "rm file.txt"}):
        errors.append("bash: 'rm file.txt' must NOT be classified read-only")

    if not BashTool.is_concurrency_safe({"command": "ls"}):
        errors.append("bash: 'ls' must be concurrency-safe")
    if BashTool.is_concurrency_safe({"command": "rm file.txt"}):
        errors.append("bash: 'rm file.txt' must NOT be concurrency-safe")

    ok, _ = BashTool.validate_input({})
    if ok:
        errors.append("bash: validate_input must reject empty command")
    ok, _ = BashTool.validate_input({"command": "echo hello"})
    if not ok:
        errors.append("bash: validate_input must accept valid command")

    if not is_silent_bash_command("rm file.txt"):
        errors.append("bash: 'rm' must be silent")
    if is_silent_bash_command("echo hello"):
        errors.append("bash: 'echo' must NOT be silent")

    interrupted_out = BashOutput(
        stdout="partial", stderr="", exit_code=130, duration_ms=100.0, interrupted=True
    )
    result = BashTool.map_result(interrupted_out)
    if "aborted" not in result:
        errors.append("bash: map_result for interrupted must mention 'aborted'")

    silent_out = BashOutput(
        stdout="", stderr="", exit_code=0, duration_ms=100.0, no_output_expected=True
    )
    result = BashTool.map_result(silent_out)
    if result != "Done":
        errors.append(
            "bash: map_result for silent success must return 'Done', got: {}".format(
                result
            )
        )

    empty_out = BashOutput(stdout="", stderr="", exit_code=0, duration_ms=100.0)
    result = BashTool.map_result(empty_out)
    if result != "(No output)":
        errors.append(
            "bash: map_result for empty non-silent must return '(No output)', got: {}".format(
                result
            )
        )

    err = ShellError("out", "err", 1, False)
    if err.stdout != "out" or err.stderr != "err" or err.exit_code != 1:
        errors.append("bash: ShellError must preserve stdout/stderr/exit_code")

    if GLOB_TOOL_NAME != "Glob":
        errors.append("glob: GLOB_TOOL_NAME must be 'Glob'")
    if GREP_TOOL_NAME != "Grep":
        errors.append("grep: GREP_TOOL_NAME must be 'Grep'")
    if BASH_TOOL_NAME != "Bash":
        errors.append("bash: BASH_TOOL_NAME must be 'Bash'")


def validate_search_and_shell_tools_contract() -> bool:
    return _load_validator()()


def main(argv: Optional[list] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["--validate-search-shell-tools"]:
        return 0 if validate_search_and_shell_tools_contract() else 1
    print(
        "Usage: python python_src/tools/validate_search_shell_tools.py "
        "--validate-search-shell-tools",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
