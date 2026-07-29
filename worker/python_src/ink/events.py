"""Event dispatch primitives for the TUI tree."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, DefaultDict, List, Optional

from .reconciler import ComponentNode


class EventPriority(Enum):
    DISCRETE = "discrete"
    CONTINUOUS = "continuous"


class EventPhase(Enum):
    CAPTURE = "capture"
    TARGET = "target"
    BUBBLE = "bubble"


@dataclass
class TerminalEvent:
    type: str
    target: Optional[ComponentNode] = None
    current_target: Optional[ComponentNode] = None
    phase: EventPhase = EventPhase.TARGET
    priority: EventPriority = EventPriority.DISCRETE
    default_prevented: bool = False
    propagation_stopped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def stop_propagation(self) -> None:
        self.propagation_stopped = True

    def prevent_default(self) -> None:
        self.default_prevented = True


@dataclass
class EmitterEvent:
    """Event payload for EventEmitter listeners."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    propagation_stopped: bool = False
    immediate_propagation_stopped: bool = False

    def stop_propagation(self) -> None:
        self.propagation_stopped = True

    def stop_immediate_propagation(self) -> None:
        self.propagation_stopped = True
        self.immediate_propagation_stopped = True


class EventEmitter:
    """Small event emitter used by the TUI runtime."""

    def __init__(self) -> None:
        self._listeners: DefaultDict[str, list[Callable[[EmitterEvent], None]]] = (
            defaultdict(list)
        )

    def on(
        self,
        event_type: str,
        listener: Callable[[EmitterEvent], None],
    ) -> Callable[[], None]:
        self._listeners[event_type].append(listener)
        return lambda: self.off(event_type, listener)

    def once(
        self,
        event_type: str,
        listener: Callable[[EmitterEvent], None],
    ) -> Callable[[], None]:
        removed = False

        def _wrapped(event: EmitterEvent) -> None:
            nonlocal removed
            if not removed:
                removed = True
                self.off(event_type, _wrapped)
            listener(event)

        return self.on(event_type, _wrapped)

    def off(self, event_type: str, listener: Callable[[EmitterEvent], None]) -> None:
        listeners = self._listeners.get(event_type)
        if not listeners:
            return
        try:
            listeners.remove(listener)
        except ValueError:
            return
        if not listeners:
            del self._listeners[event_type]

    def emit(self, event_type: str, **data: Any) -> EmitterEvent:
        event = EmitterEvent(type=event_type, data=dict(data))
        for listener in tuple(self._listeners.get(event_type, ())):
            listener(event)
            if event.immediate_propagation_stopped:
                break
        return event


@dataclass
class KeyboardEvent(TerminalEvent):
    key: str = ""
    ctrl: bool = False
    alt: bool = False
    shift: bool = False

    def __init__(
        self,
        *,
        key: str,
        target: Optional[ComponentNode] = None,
        ctrl: bool = False,
        alt: bool = False,
        shift: bool = False,
    ) -> None:
        super().__init__(type="key_down", target=target, priority=EventPriority.DISCRETE)
        self.key = key
        self.ctrl = ctrl
        self.alt = alt
        self.shift = shift


@dataclass
class MouseEvent(TerminalEvent):
    x: int = 0
    y: int = 0
    button: int = 0

    def __init__(
        self,
        *,
        event_type: str = "mouse_move",
        target: Optional[ComponentNode] = None,
        x: int = 0,
        y: int = 0,
        button: int = 0,
        priority: EventPriority = EventPriority.CONTINUOUS,
    ) -> None:
        super().__init__(type=event_type, target=target, priority=priority)
        self.x = x
        self.y = y
        self.button = button


@dataclass
class ClickEvent(MouseEvent):
    def __init__(
        self,
        *,
        target: Optional[ComponentNode] = None,
        x: int = 0,
        y: int = 0,
        button: int = 0,
    ) -> None:
        super().__init__(
            event_type="click",
            target=target,
            x=x,
            y=y,
            button=button,
            priority=EventPriority.DISCRETE,
        )


@dataclass
class FocusEvent(TerminalEvent):
    related_target: Optional[ComponentNode] = None

    def __init__(
        self,
        *,
        event_type: str,
        target: Optional[ComponentNode] = None,
        related_target: Optional[ComponentNode] = None,
    ) -> None:
        super().__init__(type=event_type, target=target, priority=EventPriority.DISCRETE)
        self.related_target = related_target


class EventDispatcher:
    """Dispatches capture/target/bubble events through the component tree."""

    def dispatch(self, event: TerminalEvent) -> TerminalEvent:
        if event.target is None:
            return event

        path = _path_to_root(event.target)
        for node in reversed(path):
            if event.propagation_stopped:
                return event
            event.current_target = node
            event.phase = EventPhase.TARGET if node is event.target else EventPhase.CAPTURE
            self._invoke(node, event, capture=True)

        for node in path:
            if event.propagation_stopped:
                return event
            event.current_target = node
            event.phase = EventPhase.TARGET if node is event.target else EventPhase.BUBBLE
            self._invoke(node, event, capture=False)

        return event

    def _invoke(
        self,
        node: ComponentNode,
        event: TerminalEvent,
        *,
        capture: bool,
    ) -> None:
        listener_name = _listener_name(event.type, capture=capture)
        listeners: list[Callable[[TerminalEvent], None]] = []

        prop_listener = node.props.get(listener_name)
        if callable(prop_listener):
            listeners.append(prop_listener)
        listeners.extend(node.event_handlers.get(listener_name, ()))

        for listener in listeners:
            listener(event)
            if event.propagation_stopped:
                break


def _listener_name(event_type: str, *, capture: bool) -> str:
    suffix = "_capture" if capture else ""
    normalized = event_type.strip().replace("-", "_")
    return f"on_{normalized}{suffix}"


def _path_to_root(node: ComponentNode) -> List[ComponentNode]:
    path: list[ComponentNode] = []
    current: Optional[ComponentNode] = node
    while current is not None:
        path.append(current)
        current = current.parent
    return path


__all__ = [
    "ClickEvent",
    "EmitterEvent",
    "EventEmitter",
    "EventDispatcher",
    "EventPhase",
    "EventPriority",
    "FocusEvent",
    "KeyboardEvent",
    "MouseEvent",
    "TerminalEvent",
]
