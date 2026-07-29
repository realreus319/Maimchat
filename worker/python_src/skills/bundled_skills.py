from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence

ContentBlock = MutableMapping[str, str]
PromptFactory = Callable[[str, object], Sequence[ContentBlock]]

_BUNDLED_SKILLS_ROOT = os.path.join(
    tempfile.gettempdir(), f"claude-code-bundled-skills-{os.getpid()}"
)


@dataclass
class SkillCommand:
    name: str
    description: str
    get_prompt_for_command: PromptFactory
    type: str = "prompt"
    aliases: Sequence[str] = field(default_factory=tuple)
    has_user_specified_description: bool = True
    allowed_tools: Sequence[str] = field(default_factory=tuple)
    argument_hint: Optional[str] = None
    arg_names: Optional[Sequence[str]] = None
    when_to_use: Optional[str] = None
    version: Optional[str] = None
    model: Optional[str] = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    content_length: int = 0
    source: str = "bundled"
    loaded_from: str = "bundled"
    hooks: Optional[Mapping[str, object]] = None
    skill_root: Optional[str] = None
    context: Optional[str] = None
    agent: Optional[str] = None
    is_enabled: Optional[Callable[[], bool]] = None
    is_hidden: bool = False
    progress_message: str = "running"
    paths: Optional[Sequence[str]] = None
    effort: Optional[str] = None

    def user_facing_name(self) -> str:
        return self.name


@dataclass
class BundledSkillDefinition:
    name: str
    description: str
    get_prompt_for_command: PromptFactory
    aliases: Sequence[str] = field(default_factory=tuple)
    when_to_use: Optional[str] = None
    argument_hint: Optional[str] = None
    allowed_tools: Sequence[str] = field(default_factory=tuple)
    model: Optional[str] = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    is_enabled: Optional[Callable[[], bool]] = None
    hooks: Optional[Mapping[str, object]] = None
    context: Optional[str] = None
    agent: Optional[str] = None
    files: Optional[Dict[str, str]] = None


_bundled_skills: List[SkillCommand] = []


def register_bundled_skill(definition: BundledSkillDefinition) -> None:
    files = dict(definition.files or {})
    skill_root = None
    prompt_factory = definition.get_prompt_for_command

    if files:
        skill_root = get_bundled_skill_extract_dir(definition.name)
        inner = prompt_factory

        @lru_cache(maxsize=1)
        def _extract_once() -> Optional[str]:
            return extract_bundled_skill_files(definition.name, files)

        def _with_extraction(args: str, context: object) -> Sequence[ContentBlock]:
            blocks = list(inner(args, context))
            extracted_dir = _extract_once()
            if extracted_dir is None:
                return blocks
            return prepend_base_dir(blocks, extracted_dir)

        prompt_factory = _with_extraction

    command = SkillCommand(
        name=definition.name,
        description=definition.description,
        aliases=tuple(definition.aliases),
        allowed_tools=tuple(definition.allowed_tools),
        argument_hint=definition.argument_hint,
        when_to_use=definition.when_to_use,
        model=definition.model,
        disable_model_invocation=definition.disable_model_invocation,
        user_invocable=definition.user_invocable,
        source="bundled",
        loaded_from="bundled",
        hooks=definition.hooks,
        skill_root=skill_root,
        context=definition.context,
        agent=definition.agent,
        is_enabled=definition.is_enabled,
        is_hidden=not definition.user_invocable,
        get_prompt_for_command=prompt_factory,
    )
    _bundled_skills.append(command)


def get_bundled_skills() -> List[SkillCommand]:
    return list(_bundled_skills)


def clear_bundled_skills() -> None:
    _bundled_skills[:] = []


def get_bundled_skill_extract_dir(skill_name: str) -> str:
    return os.path.join(_BUNDLED_SKILLS_ROOT, skill_name)


def extract_bundled_skill_files(
    skill_name: str,
    files: Mapping[str, str],
) -> Optional[str]:
    target_dir = get_bundled_skill_extract_dir(skill_name)
    try:
        write_skill_files(target_dir, files)
    except OSError:
        return None
    return target_dir


def write_skill_files(dir_path: str, files: Mapping[str, str]) -> None:
    for rel_path, content in files.items():
        target = resolve_skill_file_path(dir_path, rel_path)
        parent = os.path.dirname(target)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        safe_write_file(target, content)


def safe_write_file(path: str, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def resolve_skill_file_path(base_dir: str, rel_path: str) -> str:
    normalized = os.path.normpath(rel_path)
    if os.path.isabs(normalized):
        raise ValueError("bundled skill file path escapes skill dir")
    if ".." in normalized.replace("\\", "/").split("/"):
        raise ValueError("bundled skill file path escapes skill dir")
    return os.path.join(base_dir, normalized)


def prepend_base_dir(
    blocks: Sequence[ContentBlock],
    base_dir: str,
) -> Sequence[ContentBlock]:
    prefix = "Base directory for this skill: {}\n\n".format(base_dir)
    if blocks and blocks[0].get("type") == "text":
        first = dict(blocks[0])
        first["text"] = prefix + first.get("text", "")
        return [first] + [dict(block) for block in blocks[1:]]
    return [{"type": "text", "text": prefix}] + [dict(block) for block in blocks]
