from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class PermissionOption:
    value: str
    label: str
    feedback_type: Optional[str] = None
    placeholder: str = ""
    keybinding: Optional[str] = None


@dataclass
class PermissionPromptData:
    question: str = "Do you want to proceed?"
    options: List[PermissionOption] = field(default_factory=list)
    tool_name: str = ""
    is_mcp: bool = False
    allow_feedback: bool = True


@dataclass
class PermissionPromptState:
    focused_index: int = 0
    accept_input_mode: bool = False
    reject_input_mode: bool = False
    accept_feedback: str = ""
    reject_feedback: str = ""
    completed: bool = False
    selected_value: Optional[str] = None
    feedback_text: Optional[str] = None


class PermissionPromptRenderer:
    DEFAULT_PLACEHOLDERS: Dict[str, str] = {
        "accept": "tell Claude what to do next",
        "reject": "tell Claude what to do differently",
    }

    def __init__(self, data: PermissionPromptData) -> None:
        self._data = data
        self._state = PermissionPromptState()

    def render(self, width: int) -> str:
        lines: List[str] = []

        lines.append(self._data.question)
        lines.append("")

        for i, option in enumerate(self._data.options):
            prefix = "> " if i == self._state.focused_index else "  "
            line = f"{prefix}{option.label}"

            if i == self._state.focused_index:
                if option.feedback_type == "accept" and self._state.accept_input_mode:
                    feedback_line = self._render_feedback_input("accept", width - 4)
                    line += f"\n    {feedback_line}"
                elif option.feedback_type == "reject" and self._state.reject_input_mode:
                    feedback_line = self._render_feedback_input("reject", width - 4)
                    line += f"\n    {feedback_line}"
                elif option.feedback_type and self._data.allow_feedback:
                    line += f" (Tab for feedback)"

            lines.append(line)

        return "\n".join(lines)

    def _render_feedback_input(self, feedback_type: str, width: int) -> str:
        if feedback_type == "accept":
            text = self._state.accept_feedback
            placeholder = self.DEFAULT_PLACEHOLDERS["accept"]
        else:
            text = self._state.reject_feedback
            placeholder = self.DEFAULT_PLACEHOLDERS["reject"]

        if text:
            display = text[:width]
            return f"> {display}_"
        else:
            display = placeholder[: width - 4]
            return f"> [{display}]_"

    def move_focus(self, delta: int) -> None:
        new_index = self._state.focused_index + delta
        new_index = max(0, min(new_index, len(self._data.options) - 1))
        self._state.focused_index = new_index

        self._maybe_collapse_feedback()

    def _maybe_collapse_feedback(self) -> None:
        focused = self._get_focused_option()
        if not focused:
            return

        if focused.feedback_type != "accept" and self._state.accept_input_mode:
            if not self._state.accept_feedback.strip():
                self._state.accept_input_mode = False

        if focused.feedback_type != "reject" and self._state.reject_input_mode:
            if not self._state.reject_feedback.strip():
                self._state.reject_input_mode = False

    def _get_focused_option(self) -> Optional[PermissionOption]:
        if 0 <= self._state.focused_index < len(self._data.options):
            return self._data.options[self._state.focused_index]
        return None

    def toggle_feedback_mode(self) -> None:
        focused = self._get_focused_option()
        if not focused or not focused.feedback_type:
            return

        if focused.feedback_type == "accept":
            self._state.accept_input_mode = not self._state.accept_input_mode
        elif focused.feedback_type == "reject":
            self._state.reject_input_mode = not self._state.reject_input_mode

    def type_feedback(self, text: str) -> None:
        focused = self._get_focused_option()
        if not focused or not focused.feedback_type:
            return

        if focused.feedback_type == "accept" and self._state.accept_input_mode:
            self._state.accept_feedback += text
        elif focused.feedback_type == "reject" and self._state.reject_input_mode:
            self._state.reject_feedback += text

    def backspace_feedback(self) -> None:
        focused = self._get_focused_option()
        if not focused or not focused.feedback_type:
            return

        if focused.feedback_type == "accept" and self._state.accept_input_mode:
            self._state.accept_feedback = self._state.accept_feedback[:-1]
        elif focused.feedback_type == "reject" and self._state.reject_input_mode:
            self._state.reject_feedback = self._state.reject_feedback[:-1]

    def select(self) -> Optional[Tuple[str, Optional[str]]]:
        focused = self._get_focused_option()
        if not focused:
            return None

        self._state.selected_value = focused.value
        self._state.completed = True

        feedback = None
        if focused.feedback_type:
            if focused.feedback_type == "accept":
                feedback = self._state.accept_feedback.strip() or None
            elif focused.feedback_type == "reject":
                feedback = self._state.reject_feedback.strip() or None

        self._state.feedback_text = feedback
        return focused.value, feedback

    def is_completed(self) -> bool:
        return self._state.completed

    def get_selected_value(self) -> Optional[str]:
        return self._state.selected_value

    def get_feedback(self) -> Optional[str]:
        return self._state.feedback_text


@dataclass
class PermissionInteractionResult:
    approved: bool = False
    denied: bool = False
    cancelled: bool = False
    selected_value: Optional[str] = None
    feedback: Optional[str] = None


class PermissionInteractionFlow:
    def __init__(
        self,
        renderer: PermissionPromptRenderer,
        on_decision: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> None:
        self._renderer = renderer
        self._on_decision = on_decision
        self._result = PermissionInteractionResult()

    def process_key(self, key: str) -> bool:
        if self._renderer.is_completed():
            return False

        focused = self._renderer._get_focused_option()
        in_feedback_mode = (
            focused
            and focused.feedback_type == "accept"
            and self._renderer._state.accept_input_mode
        ) or (
            focused
            and focused.feedback_type == "reject"
            and self._renderer._state.reject_input_mode
        )

        if key == "\n" or key == "return":
            result = self._renderer.select()
            if result:
                value, feedback = result
                self._result.selected_value = value
                self._result.feedback = feedback

                if focused and focused.feedback_type == "accept":
                    self._result.approved = True
                elif focused and focused.feedback_type == "reject":
                    self._result.denied = True

                if self._on_decision:
                    self._on_decision(value, feedback)
            return False

        elif key == "\x1b" or key == "escape":
            if in_feedback_mode:
                self._renderer.toggle_feedback_mode()
            else:
                self._result.cancelled = True
                return False

        elif key == "\t" or key == "tab":
            if focused and focused.feedback_type and not in_feedback_mode:
                self._renderer.toggle_feedback_mode()

        elif key == "up":
            self._renderer.move_focus(-1)

        elif key == "down":
            self._renderer.move_focus(1)

        elif in_feedback_mode:
            if key == "\x7f" or key == "backspace":
                self._renderer.backspace_feedback()
            elif len(key) == 1 and key.isprintable():
                self._renderer.type_feedback(key)

        return True

    def get_result(self) -> PermissionInteractionResult:
        return self._result


def create_permission_prompt(data: PermissionPromptData) -> PermissionPromptRenderer:
    return PermissionPromptRenderer(data)


def create_default_permission_prompt(
    tool_name: str, is_dangerous: bool = False
) -> PermissionPromptRenderer:
    options: List[PermissionOption] = [
        PermissionOption(
            value="allow",
            label="Yes, proceed",
            feedback_type="accept" if not is_dangerous else None,
            keybinding="y",
        ),
        PermissionOption(
            value="deny",
            label="No, skip this" if not is_dangerous else "No, it's dangerous",
            feedback_type="reject" if not is_dangerous else None,
            keybinding="n",
        ),
    ]

    if is_dangerous:
        options.insert(
            0,
            PermissionOption(
                value="allow_this_once",
                label="Allow this once",
                feedback_type="accept",
                keybinding="a",
            ),
        )

    data = PermissionPromptData(
        question=f"Allow {tool_name}?",
        options=options,
        tool_name=tool_name,
        allow_feedback=True,
    )

    return PermissionPromptRenderer(data)
