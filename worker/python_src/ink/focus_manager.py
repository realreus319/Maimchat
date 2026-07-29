"""Focus state and focus/blur event dispatch for the TUI tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .events import EventDispatcher, FocusEvent
from .reconciler import ComponentNode


@dataclass
class FocusManager:
    dispatcher: EventDispatcher = field(default_factory=EventDispatcher)
    active_element: Optional[ComponentNode] = None
    _focus_stack: list[ComponentNode] = field(default_factory=list, init=False, repr=False)

    def focus(self, node: Optional[ComponentNode]) -> None:
        if node is self.active_element:
            return
        previous = self.active_element
        if previous is not None:
            self.dispatcher.dispatch(
                FocusEvent(
                    event_type="blur",
                    target=previous,
                    related_target=node,
                )
            )
        self.active_element = node
        if node is not None:
            self.dispatcher.dispatch(
                FocusEvent(
                    event_type="focus",
                    target=node,
                    related_target=previous,
                )
            )

    def blur(self) -> None:
        self.focus(None)

    def push_focus(self, node: ComponentNode) -> None:
        if self.active_element is not None and self.active_element is not node:
            self._focus_stack.append(self.active_element)
        self.focus(node)

    def pop_focus(self) -> None:
        if self._focus_stack:
            self.focus(self._focus_stack.pop())
            return
        self.blur()

    @property
    def focus_stack(self) -> tuple[ComponentNode, ...]:
        return tuple(self._focus_stack)


__all__ = ["FocusManager"]
