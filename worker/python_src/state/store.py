"""Generic observable store.

Python port of src/state/store.ts.

A minimal reactive store backed by a closure.  Consumers call ``setState``
with an updater function; if the resulting state is not ``is``-identical to
the previous state, listeners fire.  An optional ``onChange`` callback
receives both old and new state for side-effect wiring.
"""

from __future__ import annotations

from typing import Callable, Generic, Set, TypeVar

T = TypeVar("T")

Listener = Callable[[], None]
OnChange = Callable[[T, T], None]


class Store(Generic[T]):
    """Reactive state container.

    Mirrors the TS ``Store<T>`` interface exactly:
    - ``get_state()`` returns the current snapshot.
    - ``set_state(updater)`` applies an updater ``(prev) -> next``; if the
      result is identical (``is``) to prev, listeners are **not** notified.
    - ``subscribe(listener)`` registers a callback and returns an unsubscribe
      function.
    - Optional ``on_change`` receives ``(new_state, old_state)`` on every
      non-identity transition.
    """

    def __init__(
        self,
        initial_state: T,
        on_change: OnChange[T] | None = None,
    ) -> None:
        self._state: T = initial_state
        self._listeners: set[Listener] = set()
        self._on_change: OnChange[T] | None = on_change

    def get_state(self) -> T:
        return self._state

    def set_state(self, updater: Callable[[T], T]) -> None:
        prev = self._state
        next_state = updater(prev)
        if next_state is prev:
            return
        self._state = next_state
        if self._on_change is not None:
            self._on_change(next_state, prev)
        for listener in list(self._listeners):
            listener()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe
