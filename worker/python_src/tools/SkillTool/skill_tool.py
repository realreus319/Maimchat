from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

if __package__ in (None, ""):
    _here = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(_here)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from python_src.plugins.builtin_plugins import (  # type: ignore[import-not-found]
        BuiltinPluginDefinition,
        clear_builtin_plugins,
        get_builtin_plugin_skill_commands,
        get_plugin_error_message,
        load_plugin_directory,
        register_builtin_plugin,
    )
    from python_src.skills.bundled_skills import (  # type: ignore[import-not-found]
        BundledSkillDefinition,
        SkillCommand,
        clear_bundled_skills,
        get_bundled_skills,
        register_bundled_skill,
    )
    from python_src.skills.load_skills_dir import (  # type: ignore[import-not-found]
        activate_conditional_skills_for_paths,
        add_skill_directories,
        clear_dynamic_skills,
        discover_skill_dirs_for_paths,
        get_conditional_skill_count,
        get_dynamic_skills,
        on_dynamic_skills_loaded,
    )
    from python_src.tools.SkillTool.constants import (  # type: ignore[import-not-found]
        SKILL_TOOL_NAME,
    )
else:
    from ...plugins.builtin_plugins import (
        BuiltinPluginDefinition,
        clear_builtin_plugins,
        get_builtin_plugin_skill_commands,
        get_plugin_error_message,
        load_plugin_directory,
        register_builtin_plugin,
    )
    from ...skills.bundled_skills import (
        BundledSkillDefinition,
        SkillCommand,
        clear_bundled_skills,
        get_bundled_skills,
        register_bundled_skill,
    )
    from ...skills.load_skills_dir import (
        activate_conditional_skills_for_paths,
        add_skill_directories,
        clear_dynamic_skills,
        discover_skill_dirs_for_paths,
        get_conditional_skill_count,
        get_dynamic_skills,
        on_dynamic_skills_loaded,
    )
    from .constants import SKILL_TOOL_NAME


@dataclass
class ValidationResult:
    result: bool
    message: str = ""
    error_code: int = 0


_SAFE_SKILL_PROPERTIES: frozenset[str] = frozenset({
    "name",
    "description",
    "get_prompt_for_command",
    "type",
    "aliases",
    "has_user_specified_description",
    "allowed_tools",
    "argument_hint",
    "arg_names",
    "when_to_use",
    "version",
    "model",
    "disable_model_invocation",
    "user_invocable",
    "content_length",
    "source",
    "loaded_from",
    "hooks",
    "skill_root",
    "context",
    "agent",
    "is_enabled",
    "is_hidden",
    "progress_message",
    "paths",
    "effort",
})


def skill_has_only_safe_properties(command: SkillCommand) -> bool:
    return set(command.keys()).issubset(_SAFE_SKILL_PROPERTIES)


class SkillTool:
    name = SKILL_TOOL_NAME

    @staticmethod
    def get_all_commands(
        base_commands: Sequence[SkillCommand],
        dynamic_commands: Optional[Sequence[SkillCommand]] = None,
    ) -> List[SkillCommand]:
        seen: Dict[str, SkillCommand] = {}
        for command in list(base_commands) + list(dynamic_commands or []):
            seen[command.name] = command
        return list(seen.values())

    @staticmethod
    def validate_input(
        skill: str,
        commands: Sequence[SkillCommand],
    ) -> ValidationResult:
        trimmed = skill.strip()
        if not trimmed:
            return ValidationResult(False, "Invalid skill format: {}".format(skill), 1)
        normalized = trimmed[1:] if trimmed.startswith("/") else trimmed
        command = SkillTool.find_command(normalized, commands)
        if command is None:
            return ValidationResult(False, "Unknown skill: {}".format(normalized), 2)
        if command.disable_model_invocation:
            return ValidationResult(
                False,
                "Skill {} cannot be used with {} tool due to disable-model-invocation".format(
                    normalized,
                    SKILL_TOOL_NAME,
                ),
                4,
            )
        if command.type != "prompt":
            return ValidationResult(
                False,
                "Skill {} is not a prompt-based skill".format(normalized),
                5,
            )
        return ValidationResult(True)

    @staticmethod
    def call(
        skill: str,
        args: str,
        commands: Sequence[SkillCommand],
        tool_use_context: Optional[dict] = None,
    ) -> Dict[str, object]:
        normalized = (
            skill.strip()[1:] if skill.strip().startswith("/") else skill.strip()
        )
        command = SkillTool.find_command(normalized, commands)
        if command is None:
            raise ValueError("Unknown skill: {}".format(normalized))
        if command.context == "fork":
            blocks = command.get_prompt_for_command(args, tool_use_context or {})
            result_text = "\n".join(block.get("text", "") for block in blocks)
            return {
                "success": True,
                "commandName": normalized,
                "status": "forked",
                "agentId": "skill-agent",
                "result": result_text,
            }
        return {
            "success": True,
            "commandName": normalized,
            "allowedTools": list(command.allowed_tools),
            "model": command.model,
            "status": "inline",
        }

    @staticmethod
    def find_command(
        name: str,
        commands: Sequence[SkillCommand],
    ) -> Optional[SkillCommand]:
        for command in commands:
            if command.name == name:
                return command
            if name in command.aliases:
                return command
        return None


def validate_plugins_and_skills_contract() -> Tuple[bool, Tuple[str, ...]]:
    errors: List[str] = []

    clear_bundled_skills()
    clear_builtin_plugins()
    clear_dynamic_skills()

    register_bundled_skill(
        BundledSkillDefinition(
            name="bundled-skill",
            description="Bundled skill",
            files={"docs/reference.txt": "hello"},
            get_prompt_for_command=lambda args, context: [
                {"type": "text", "text": "Prompt {}".format(args)}
            ],
        )
    )
    bundled = get_bundled_skills()
    if len(bundled) != 1 or bundled[0].source != "bundled":
        errors.append("bundled skill registry did not preserve source metadata")

    register_builtin_plugin(
        BuiltinPluginDefinition(
            name="builtin-demo",
            description="Builtin demo plugin",
            default_enabled=True,
            skills=(
                BundledSkillDefinition(
                    name="plugin-skill",
                    description="Plugin skill",
                    get_prompt_for_command=lambda args, context: [
                        {"type": "text", "text": "plugin"}
                    ],
                ),
            ),
        )
    )
    plugin_commands = get_builtin_plugin_skill_commands()
    if len(plugin_commands) != 1 or plugin_commands[0].name != "plugin-skill":
        errors.append("builtin plugin skill command surface mismatch")

    with tempfile.TemporaryDirectory() as tmpdir:
        broken_plugin_dir = os.path.join(tmpdir, "broken-plugin")
        os.makedirs(broken_plugin_dir)
        with open(
            os.path.join(broken_plugin_dir, "plugin.json"), "w", encoding="utf-8"
        ) as handle:
            handle.write('{"name": "", "description": 1}')
        broken = load_plugin_directory(broken_plugin_dir, source="fixture")
        if not broken.errors or broken.errors[0].type != "manifest-validation-error":
            errors.append("broken plugin did not produce validation error")
        else:
            message = get_plugin_error_message(broken.errors[0])
            if "Manifest validation failed" not in message:
                errors.append("broken plugin validation message drifted")

    with tempfile.TemporaryDirectory() as project_root:
        touched_file = os.path.join(project_root, "src", "module.py")
        os.makedirs(os.path.dirname(touched_file))
        with open(touched_file, "w", encoding="utf-8") as handle:
            handle.write("print('x')\n")
        nested_skill_dir = os.path.join(
            project_root, "src", ".claude_py", "skills", "path-aware"
        )
        os.makedirs(nested_skill_dir)
        skill_document = (
            "---\n"
            "description: Path aware skill\n"
            "paths: src/*.py\n"
            "context: fork\n"
            "agent: planner\n"
            "allowed-tools: Read,Grep\n"
            "---\n"
            "Use ${CLAUDE_SKILL_DIR} and ${CLAUDE_SESSION_ID} with $ARGUMENTS"
        )
        with open(
            os.path.join(nested_skill_dir, "SKILL.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write(skill_document)

        notifications: List[str] = []
        unsubscribe = on_dynamic_skills_loaded(lambda: notifications.append("loaded"))
        try:
            dirs = discover_skill_dirs_for_paths([touched_file], project_root)
            if dirs != [os.path.join(project_root, "src", ".claude_py", "skills")]:
                errors.append("dynamic discovery did not return nested skills dir")
            add_skill_directories(dirs)
            if get_dynamic_skills():
                errors.append("conditional skills should not load before activation")
            if get_conditional_skill_count() != 1:
                errors.append("conditional skills were not staged for activation")
            activated = activate_conditional_skills_for_paths(
                [touched_file], project_root
            )
            if activated != ["path-aware"]:
                errors.append("conditional skill activation mismatch")
            if notifications.count("loaded") != 2:
                errors.append("dynamic skill notifications should fire twice")
            dynamic_commands = get_dynamic_skills()
            if len(dynamic_commands) != 1:
                errors.append("activated dynamic skill missing from registry")
            else:
                validation = SkillTool.validate_input(
                    "path-aware",
                    SkillTool.get_all_commands(
                        bundled + plugin_commands, dynamic_commands
                    ),
                )
                if not validation.result:
                    errors.append("SkillTool rejected activated dynamic skill")
                forked = SkillTool.call(
                    "path-aware",
                    "demo-arg",
                    dynamic_commands,
                    {"session_id": "session-123"},
                )
                if forked.get("status") != "forked":
                    errors.append("forked dynamic skill call did not preserve context")
                result_text = str(forked.get("result", ""))
                if "session-123" not in result_text or "demo-arg" not in result_text:
                    errors.append(
                        "dynamic skill prompt injection lost source-backed substitutions"
                    )
        finally:
            unsubscribe()

    clear_bundled_skills()
    clear_builtin_plugins()
    clear_dynamic_skills()
    return (len(errors) == 0, tuple(errors))
