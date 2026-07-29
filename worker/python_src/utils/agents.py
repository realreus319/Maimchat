from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .config import get_claude_config_home

AGENTS_FILE_NAME = "agents.json"

_TYPE_ALIASES = {
    "brief": "brief",
    "claude-code": "claude-code-guide",
    "claude-code-guide": "claude-code-guide",
    "claudecodeguide": "claude-code-guide",
    "dream": "dream",
    "explore": "explore",
    "explorer": "explore",
    "general": "general-purpose",
    "general-purpose": "general-purpose",
    "generalpurpose": "general-purpose",
    "guide": "claude-code-guide",
    "local": "local-subagent",
    "local-subagent": "local-subagent",
    "plan": "planner",
    "planner": "planner",
    "skill-agent": "skill-agent",
    "statusline": "statusline-setup",
    "statusline-setup": "statusline-setup",
    "subagent": "local-subagent",
    "verification": "verification",
    "verify": "verification",
}


def slugify_agent_name(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def normalize_agent_type(value: str | None) -> str:
    normalized = slugify_agent_name(value)
    if not normalized:
        return "local-subagent"
    return _TYPE_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    source: str
    description: str = ""
    prompt: str | None = None
    model: str | None = None
    color: str | None = None
    tools: tuple[str, ...] = ()
    agent_type: str | None = None

    @property
    def lookup_key(self) -> str:
        return slugify_agent_name(self.name)

    @property
    def resolved_agent_type(self) -> str:
        if self.agent_type:
            return normalize_agent_type(self.agent_type)
        if self.source == "builtin":
            return normalize_agent_type(self.name)
        return self.lookup_key or "local-subagent"

    @property
    def display_model(self) -> str:
        if not isinstance(self.model, str) or not self.model.strip():
            return "inherit"
        return self.model.strip()

    def to_persisted_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.description:
            payload["description"] = self.description
        if self.prompt:
            payload["prompt"] = self.prompt
        if self.model:
            payload["model"] = self.model
        if self.color:
            payload["color"] = self.color
        if self.tools:
            payload["tools"] = list(self.tools)
        if self.agent_type:
            payload["type"] = self.agent_type
        return payload

    def to_public_payload(self) -> dict[str, Any]:
        payload = self.to_persisted_payload()
        payload.update(
            {
                "name": self.name,
                "source": self.source,
                "resolvedType": self.resolved_agent_type,
                "displayModel": self.display_model,
            }
        )
        return payload


@dataclass(frozen=True)
class AgentRegistry:
    builtin_agents: tuple[AgentDefinition, ...]
    custom_agents: tuple[AgentDefinition, ...]
    inline_agents: tuple[AgentDefinition, ...]

    @property
    def all_agents(self) -> tuple[AgentDefinition, ...]:
        custom_by_key = {
            agent.lookup_key: agent
            for agent in self.custom_agents
        }
        inline_by_key = {
            agent.lookup_key: agent
            for agent in self.inline_agents
        }
        merged_builtins: list[AgentDefinition] = []
        for agent in self.builtin_agents:
            override = inline_by_key.get(agent.lookup_key) or custom_by_key.get(
                agent.lookup_key
            )
            merged_builtins.append(override or agent)
        merged_custom: list[AgentDefinition] = []
        emitted = {agent.lookup_key for agent in self.builtin_agents}
        for collection in (self.custom_agents, self.inline_agents):
            for agent in collection:
                if agent.lookup_key in emitted:
                    continue
                if agent.lookup_key in inline_by_key and agent.source != "inline":
                    continue
                emitted.add(agent.lookup_key)
                merged_custom.append(agent)
        return tuple(merged_builtins + merged_custom)

    def resolve(self, value: str | None) -> AgentDefinition | None:
        lookup = slugify_agent_name(value)
        if not lookup:
            return None
        for agent in self.all_agents:
            if agent.lookup_key == lookup:
                return agent
        for agent in self.builtin_agents:
            if normalize_agent_type(agent.agent_type or agent.name) == normalize_agent_type(
                lookup
            ):
                override = self._override_for_builtin(agent.lookup_key)
                return override or agent
        return None

    def _override_for_builtin(self, key: str) -> AgentDefinition | None:
        for agent in self.inline_agents:
            if agent.lookup_key == key:
                return agent
        for agent in self.custom_agents:
            if agent.lookup_key == key:
                return agent
        return None


_BUILTIN_AGENTS = (
    AgentDefinition(
        name="Explore",
        source="builtin",
        description="Quickly search the codebase for patterns and file locations.",
        model="haiku",
        agent_type="explore",
    ),
    AgentDefinition(
        name="general-purpose",
        source="builtin",
        description="General-purpose coding agent.",
        agent_type="general-purpose",
    ),
    AgentDefinition(
        name="Plan",
        source="builtin",
        description="Planning-focused agent for decomposition and sequencing.",
        agent_type="planner",
    ),
    AgentDefinition(
        name="statusline-setup",
        source="builtin",
        description="Statusline setup helper.",
        model="sonnet",
        agent_type="statusline-setup",
    ),
)


def get_agents_file_path(config_home: str | None = None) -> str:
    home = config_home or get_claude_config_home()
    return os.path.join(home, AGENTS_FILE_NAME)


def parse_agents_payload(
    payload: object,
    *,
    source_name: str,
    source_type: str,
) -> tuple[AgentDefinition, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, Mapping):
        raise ValueError(f"{source_name} must be a JSON object mapping names to agents")
    agents: list[AgentDefinition] = []
    for raw_name, raw_definition in payload.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(f"{source_name} contains an empty agent name")
        name = raw_name.strip()
        if not isinstance(raw_definition, Mapping):
            raise ValueError(
                f"{source_name} agent '{name}' must be a JSON object"
            )
        description = _optional_string(raw_definition.get("description"))
        prompt = _optional_string(raw_definition.get("prompt"))
        model = _optional_string(raw_definition.get("model"))
        color = _optional_string(raw_definition.get("color"))
        agent_type = _optional_string(
            raw_definition.get("type")
            or raw_definition.get("agentType")
            or raw_definition.get("agent")
        )
        tools = _normalize_tools(raw_definition.get("tools"), source_name, name)
        agents.append(
            AgentDefinition(
                name=name,
                source=source_type,
                description=description or "",
                prompt=prompt,
                model=model,
                color=color,
                tools=tools,
                agent_type=agent_type,
            )
        )
    agents.sort(key=lambda agent: agent.name.casefold())
    return tuple(agents)


def parse_agents_json(raw: str, *, source_name: str, source_type: str) -> tuple[AgentDefinition, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON for {source_name}: {exc.msg}") from exc
    return parse_agents_payload(payload, source_name=source_name, source_type=source_type)


def load_custom_agents(*, config_home: str | None = None) -> tuple[AgentDefinition, ...]:
    path = get_agents_file_path(config_home)
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError(f"Could not read agents file {path}: {exc}") from exc
    if not content.strip():
        return ()
    return parse_agents_json(content, source_name=path, source_type="custom")


def save_custom_agents(
    agents: Iterable[AgentDefinition],
    *,
    config_home: str | None = None,
) -> None:
    path = get_agents_file_path(config_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        agent.name: agent.to_persisted_payload()
        for agent in sorted(agents, key=lambda item: item.name.casefold())
    }
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path),
        prefix=".agents.tmp.",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def build_agent_registry(
    *,
    config_home: str | None = None,
    inline_agents: Sequence[AgentDefinition] | None = None,
) -> AgentRegistry:
    return AgentRegistry(
        builtin_agents=_BUILTIN_AGENTS,
        custom_agents=load_custom_agents(config_home=config_home),
        inline_agents=tuple(inline_agents or ()),
    )


def format_agents_listing(registry: AgentRegistry) -> str:
    lines = [f"{len(registry.all_agents)} active agents", "", "Built-in agents:"]
    builtins_by_key = {agent.lookup_key: agent for agent in registry.builtin_agents}
    all_by_key = {agent.lookup_key: agent for agent in registry.all_agents}
    for builtin in registry.builtin_agents:
        agent = all_by_key.get(builtin.lookup_key, builtin)
        lines.append(f"  {agent.name} · {agent.display_model}")
    custom_agents = [
        agent
        for agent in registry.all_agents
        if agent.lookup_key not in builtins_by_key
    ]
    if custom_agents:
        lines.extend(["", "Custom agents:"])
        for agent in custom_agents:
            lines.append(f"  {agent.name} · {agent.display_model}")
    return "\n".join(lines) + "\n"


def upsert_custom_agent(
    name: str,
    *,
    config_home: str | None = None,
    description: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    color: str | None = None,
    tools: Sequence[str] | None = None,
    agent_type: str | None = None,
) -> AgentDefinition:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Agent name must be a non-empty string")
    existing_by_key = {
        agent.lookup_key: agent
        for agent in load_custom_agents(config_home=config_home)
    }
    key = slugify_agent_name(normalized_name)
    current = existing_by_key.get(key)
    updated = AgentDefinition(
        name=normalized_name,
        source="custom",
        description=description if description is not None else (current.description if current else ""),
        prompt=prompt if prompt is not None else (current.prompt if current else None),
        model=model if model is not None else (current.model if current else None),
        color=color if color is not None else (current.color if current else None),
        tools=tuple(tools) if tools is not None else (current.tools if current else ()),
        agent_type=agent_type if agent_type is not None else (current.agent_type if current else None),
    )
    existing_by_key[key] = updated
    save_custom_agents(existing_by_key.values(), config_home=config_home)
    return updated


def delete_custom_agent(name: str, *, config_home: str | None = None) -> bool:
    key = slugify_agent_name(name)
    if not key:
        raise ValueError("Agent name must be a non-empty string")
    existing_by_key = {
        agent.lookup_key: agent
        for agent in load_custom_agents(config_home=config_home)
    }
    removed = existing_by_key.pop(key, None)
    if removed is None:
        return False
    save_custom_agents(existing_by_key.values(), config_home=config_home)
    return True


def list_custom_agents(*, config_home: str | None = None) -> tuple[AgentDefinition, ...]:
    return load_custom_agents(config_home=config_home)


def get_custom_agent(name: str, *, config_home: str | None = None) -> AgentDefinition | None:
    key = slugify_agent_name(name)
    for agent in load_custom_agents(config_home=config_home):
        if agent.lookup_key == key:
            return agent
    return None


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_tools(
    value: object,
    source_name: str,
    agent_name: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = [
            item.strip()
            for item in value.replace(",", " ").split()
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(
                    f"{source_name} agent '{agent_name}' field 'tools' must contain strings"
                )
            raw_items.append(item.strip())
    else:
        raise ValueError(
            f"{source_name} agent '{agent_name}' field 'tools' must be a string or array"
        )
    tools: list[str] = []
    for item in raw_items:
        if not item or item in tools:
            continue
        tools.append(item)
    return tuple(tools)
