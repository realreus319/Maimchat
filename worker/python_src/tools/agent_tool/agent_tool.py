from __future__ import annotations

import os
import sys
import time
from importlib import import_module
from typing import Callable, Dict, Optional

if __package__ in (None, ""):
    _here = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(_here)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    _agents = import_module("python_src.tasks.agent_orchestration")
else:
    from ...tasks import agent_orchestration as _agents


AgentOrchestrationManager = _agents.AgentOrchestrationManager
AgentToolResult = _agents.AgentToolResult
make_agent_result = _agents.make_agent_result
TASK_STATUS_COMPLETED = _agents.TASK_STATUS_COMPLETED
TASK_STATUS_FAILED = _agents.TASK_STATUS_FAILED


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_optional_non_empty_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_non_empty_string(value, field_name)


def _require_callable(value: object, field_name: str) -> Callable:
    if not callable(value):
        raise TypeError(f"{field_name} must be callable")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _require_non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return value


def _require_positive_number(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return normalized


def _require_optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _slugify_agent_type(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "local-subagent"
    normalized = "".join(
        character.lower() if character.isalnum() else "-"
        for character in value.strip()
    )
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    normalized = normalized.strip("-") or "local-subagent"
    aliases = {
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
        "subagent": "local-subagent",
        "verification": "verification",
        "verify": "verification",
    }
    return aliases.get(normalized, normalized)


class AgentTool:
    name = "Agent"

    def __init__(self, manager: Optional[AgentOrchestrationManager] = None) -> None:
        self.manager = manager if manager is not None else AgentOrchestrationManager()

    def call(
        self,
        *,
        agent_id: str,
        description: str,
        prompt: str,
        output_file: str,
        runner: Callable,
        should_run_async: bool,
        agent_type: str = "local-subagent",
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        tool_use_id: Optional[str] = None,
        auto_background_ms: int = 0,
        timeout: float = 5.0,
    ) -> Dict[str, object]:
        validated_agent_id = _require_non_empty_string(agent_id, "agent_id")
        validated_description = _require_non_empty_string(description, "description")
        validated_prompt = _require_non_empty_string(prompt, "prompt")
        validated_output_file = _require_non_empty_string(output_file, "output_file")
        validated_runner = _require_callable(runner, "runner")
        validated_should_run_async = _require_bool(
            should_run_async,
            "should_run_async",
        )
        validated_agent_type = _slugify_agent_type(agent_type)
        validated_cwd = _require_optional_non_empty_string(cwd, "cwd")
        validated_model = _require_optional_non_empty_string(model, "model")
        validated_max_tokens = _require_optional_positive_int(max_tokens, "max_tokens")
        validated_auto_background_ms = _require_non_negative_int(
            auto_background_ms,
            "auto_background_ms",
        )
        validated_timeout = _require_positive_number(timeout, "timeout")

        if validated_should_run_async:
            task = self.manager.register_async_agent(
                agent_id=validated_agent_id,
                description=validated_description,
                prompt=validated_prompt,
                runner=validated_runner,
                output_file=validated_output_file,
                tool_use_id=tool_use_id,
                agent_type=validated_agent_type,
                cwd=validated_cwd,
                model=validated_model,
                max_tokens=validated_max_tokens,
            )
            return self._state_payload(task, status="async_launched")

        handle = self.manager.register_agent_foreground(
            agent_id=validated_agent_id,
            description=validated_description,
            prompt=validated_prompt,
            runner=validated_runner,
            output_file=validated_output_file,
            tool_use_id=tool_use_id,
            agent_type=validated_agent_type,
            cwd=validated_cwd,
            model=validated_model,
            max_tokens=validated_max_tokens,
            auto_background_ms=validated_auto_background_ms,
        )
        deadline = time.time() + validated_timeout
        while time.time() < deadline:
            state = self.manager.get_agent_task(handle.task_id)
            if state.is_backgrounded and not state.is_terminal:
                return self._state_payload(state, status="async_launched")
            if state.status == TASK_STATUS_COMPLETED and state.result is not None:
                return self._state_payload(state, status="completed")
            if state.status == TASK_STATUS_FAILED:
                return self._state_payload(state, status="failed")
            time.sleep(0.01)
        raise TimeoutError("agent tool call timed out")

    def _state_payload(
        self,
        state: _agents.AgentTaskState,
        *,
        status: str,
    ) -> Dict[str, object]:
        if status == "completed" and state.result is not None:
            payload: Dict[str, object] = dict(state.result.as_tool_payload())
        else:
            payload = {"status": status}
        payload["progress"] = _agents.agent_progress_payload(state.progress)
        payload["summary"] = _agents.agent_summary_payload(
            status=status,
            agent_id=state.agent_id,
            agent_type=state.agent_type,
            progress=state.progress,
            is_backgrounded=state.is_backgrounded,
            total_tool_use_count=(
                state.result.total_tool_use_count if state.result is not None else None
            ),
            total_tokens=(state.result.total_tokens if state.result is not None else None),
            total_duration_ms=(
                state.result.total_duration_ms if state.result is not None else None
            ),
            content=(state.result.content if state.result is not None else ()),
            error=state.error,
        )

        payload.update(
            {
                "agentId": state.agent_id,
                "agentType": state.agent_type,
                "description": state.description,
                "prompt": state.prompt,
                "outputFile": state.output_file,
                "taskId": state.task_id,
            }
        )
        if state.cwd is not None:
            payload["cwd"] = state.cwd
        if state.model is not None:
            payload["model"] = state.model
        if state.max_tokens is not None:
            payload["maxTokens"] = state.max_tokens
        if status == "failed":
            payload["error"] = state.error or "agent execution failed"
        return payload


__all__ = [
    "AgentOrchestrationManager",
    "AgentTool",
    "AgentToolResult",
    "make_agent_result",
]
