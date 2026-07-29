from __future__ import annotations

# pyright: reportMissingImports=false

import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from python_src.components.permission_prompt import (
    PermissionInteractionFlow,
    PermissionInteractionResult,
    PermissionPromptData,
    PermissionPromptRenderer,
)
from python_src.components.prompt_input import (
    PromptInputConfig,
    PromptInputData,
    PromptInputRenderer,
    PromptInputState,
    PromptInteractionFlow,
    PromptInteractionResult,
)
from python_src.components.status_line import StatusLineData, StatusLineRenderer


@dataclass
class TUIInteractionState:
    prompt_input: PromptInputData = field(default_factory=PromptInputData)
    permission_prompt: Optional[PermissionPromptData] = None
    status_line: Optional[StatusLineData] = None
    mode: str = "idle"
    pending_approval: bool = False
    prompt_history: List[str] = field(default_factory=list)


class TUIInteractionFlow:
    def __init__(self, state: TUIInteractionState) -> None:
        self._state = state
        self._prompt_renderer: Optional[PromptInputRenderer] = None
        self._permission_renderer: Optional[PermissionPromptRenderer] = None
        self._status_renderer: Optional[StatusLineRenderer] = None
        self._prompt_result: Optional[PromptInteractionResult] = None
        self._permission_result: Optional[PermissionInteractionResult] = None

        if state.prompt_input:
            state.prompt_input.state.history_entries = state.prompt_history
            self._prompt_renderer = PromptInputRenderer(state.prompt_input)

        if state.permission_prompt:
            self._permission_renderer = PermissionPromptRenderer(
                state.permission_prompt
            )
            self._state.pending_approval = True

        if state.status_line:
            self._status_renderer = StatusLineRenderer(state.status_line)

    def render(self, width: int, height: int) -> str:
        lines: List[str] = []

        if self._state.mode == "prompt" and self._prompt_renderer:
            prompt_output = self._prompt_renderer.render(width)
            if height > 0:
                prompt_lines = prompt_output.split("\n")
                if len(prompt_lines) > height:
                    prompt_output = "\n".join(prompt_lines[-height:])
            lines.append(prompt_output)
        elif self._state.mode == "approval" and self._permission_renderer:
            perm_output = self._permission_renderer.render(width)
            lines.append(perm_output)
        else:
            lines.append("[TUI Interaction Flow]")
            lines.append(f"Mode: {self._state.mode}")

        if self._status_renderer:
            status_line = self._status_renderer.render(width)
            lines.append("")
            lines.append("-" * width)
            lines.append(status_line)

        result = "\n".join(lines)
        if len(result.split("\n")) > height:
            result_lines = result.split("\n")[:height]
            result = "\n".join(result_lines)

        return result

    def transition_to_prompt(self, placeholder: str = "") -> None:
        self._state.mode = "prompt"
        self._state.pending_approval = False

        config = PromptInputConfig()
        prompt_state = PromptInputState(history_entries=self._state.prompt_history)
        self._state.prompt_input = PromptInputData(
            state=prompt_state,
            config=config,
            placeholder=placeholder,
        )
        self._prompt_renderer = PromptInputRenderer(self._state.prompt_input)
        self._permission_renderer = None
        self._permission_result = None

    def transition_to_approval(self, permission_data: PermissionPromptData) -> None:
        self._state.mode = "approval"
        self._state.pending_approval = True
        self._state.permission_prompt = permission_data
        self._permission_renderer = PermissionPromptRenderer(permission_data)

    def transition_to_idle(self) -> None:
        self._state.mode = "idle"
        self._state.pending_approval = False

    def update_status(self, status_data: StatusLineData) -> None:
        self._state.status_line = status_data
        self._status_renderer = StatusLineRenderer(status_data)

    def handle_prompt_submit(self, text: str) -> PromptInteractionResult:
        entry = text.strip()
        if entry and (
            not self._state.prompt_history or self._state.prompt_history[-1] != entry
        ):
            self._state.prompt_history.append(entry)
        result = PromptInteractionResult(submitted=True, text=text)
        self._prompt_result = result
        self.transition_to_idle()
        return result

    def set_prompt_history(self, entries: List[str]) -> None:
        self._state.prompt_history = [entry.strip() for entry in entries if entry.strip()]
        self._state.prompt_input.state.history_entries = self._state.prompt_history

    def get_prompt_history(self) -> tuple[str, ...]:
        return tuple(self._state.prompt_history)

    def set_prompt_input(self, text: str, cursor_offset: Optional[int] = None) -> None:
        if self._state.mode != "prompt" or self._prompt_renderer is None:
            self.transition_to_prompt("Type a message or /exit")
        state = self._state.prompt_input.state
        state.input_text = text
        if cursor_offset is None:
            cursor_offset = len(text)
        state.cursor_offset = max(0, min(cursor_offset, len(text)))

    def get_prompt_input(self) -> tuple[str, int]:
        state = self._state.prompt_input.state
        return (state.input_text, state.cursor_offset)

    def clear_prompt_input(self) -> None:
        self.set_prompt_input("", 0)

    def handle_permission_decision(
        self, value: str, feedback: Optional[str] = None
    ) -> PermissionInteractionResult:
        approved = value in ("allow", "allow_this_once", "allow_all")
        denied = value in ("deny", "deny_all")

        result = PermissionInteractionResult(
            approved=approved,
            denied=denied,
            selected_value=value,
            feedback=feedback,
        )
        self._permission_result = result
        self._state.pending_approval = False
        self.transition_to_idle()
        return result

    def get_state(self) -> TUIInteractionState:
        return self._state

    def is_pending_approval(self) -> bool:
        return self._state.pending_approval

    def get_mode(self) -> str:
        return self._state.mode


def validate_tui_interaction_contract() -> Tuple[bool, Tuple[str, ...]]:
    errors: List[str] = []

    state = TUIInteractionState(mode="idle")
    flow = TUIInteractionFlow(state)

    if flow.get_mode() != "idle":
        errors.append("Initial mode should be idle")

    flow.transition_to_prompt("Enter command:")
    if flow.get_mode() != "prompt":
        errors.append("Transition to prompt failed")

    result = flow.handle_prompt_submit("test command")
    if not result.submitted:
        errors.append("Prompt submission should return submitted=True")
    if result.text != "test command":
        errors.append("Prompt submission should preserve text")
    if flow.get_mode() != "idle":
        errors.append("After submit should return to idle")

    from python_src.components.permission_prompt import PermissionOption

    perm_data = PermissionPromptData(
        question="Allow test?",
        options=[
            PermissionOption(value="allow", label="Yes", feedback_type="accept"),
            PermissionOption(value="deny", label="No", feedback_type="reject"),
        ],
    )
    flow.transition_to_approval(perm_data)
    if flow.get_mode() != "approval":
        errors.append("Transition to approval failed")
    if not flow.is_pending_approval():
        errors.append("Approval mode should have pending_approval=True")

    result = flow.handle_permission_decision("allow", "proceed with care")
    if not result.approved:
        errors.append("Allow decision should have approved=True")
    if result.feedback != "proceed with care":
        errors.append("Approval should preserve feedback")
    if flow.is_pending_approval():
        errors.append("After decision should clear pending_approval")

    result = flow.handle_permission_decision("deny")
    if not result.denied:
        errors.append("Deny decision should have denied=True")

    render_output = flow.render(80, 24)
    if not isinstance(render_output, str):
        errors.append("Render should return string")

    return (not errors, tuple(errors))


if __name__ == "__main__":
    passed, errors = validate_tui_interaction_contract()
    if passed:
        print("TUI interaction contract: PASSED")
    else:
        print("TUI interaction contract: FAILED")
        for error in errors:
            print(f"  - {error}")
    sys.exit(0 if passed else 1)
