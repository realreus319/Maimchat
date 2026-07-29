"""TUI reconciler for managing component tree updates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from .layout.engine import LayoutNode

LifecycleCallback = Callable[..., None]


def _call_if_callable(callback: Any, *args: Any) -> None:
    if callable(callback):
        callback(*args)


@dataclass(eq=False)
class ComponentNode:
    """A node in the component tree."""

    type: str
    props: Dict[str, Any] = field(default_factory=dict)
    children: List["ComponentNode"] = field(default_factory=list)
    layout_node: Optional[LayoutNode] = None
    key: Optional[str] = None
    dirty: bool = True
    mounted: bool = False
    parent: Optional["ComponentNode"] = None
    event_handlers: Dict[str, List[LifecycleCallback]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []
        for child in self.children:
            child.parent = self

    def add_event_listener(self, name: str, listener: LifecycleCallback) -> None:
        self.event_handlers.setdefault(name, []).append(listener)

    def remove_event_listener(self, name: str, listener: LifecycleCallback) -> None:
        listeners = self.event_handlers.get(name)
        if listeners is None:
            return
        self.event_handlers[name] = [item for item in listeners if item is not listener]
        if not self.event_handlers[name]:
            del self.event_handlers[name]


class Reconciler:
    """Reconciler for managing component updates.

    This remains far simpler than React Fiber, but it now performs keyed child
    reconciliation, dirty-subtree tracking, and mount/update/unmount lifecycle
    dispatch instead of acting as a no-op dirty flag.
    """

    def __init__(self) -> None:
        self._root: Optional[ComponentNode] = None
        self._dirty_nodes: Set[ComponentNode] = set()
        self._intrinsic_dirty_nodes: Set[ComponentNode] = set()

    def mount(self, node: ComponentNode) -> None:
        """Mount the component tree."""
        if self._root is not None and self._root is not node:
            self.unmount(self._root)
        self._root = node
        self._attach_subtree(node, parent=None, invoke_mount=True)

    def update(
        self,
        node: ComponentNode,
        new_props: Dict[str, Any],
        new_children: Optional[Sequence[ComponentNode]] = None,
        *,
        new_layout_node: Optional[LayoutNode] = None,
    ) -> None:
        """Update a node with new props and optional child tree."""
        old_props = dict(node.props)
        changed = node.props != new_props
        if changed:
            node.props = dict(new_props)
            _call_if_callable(node.props.get("on_update"), node, old_props, new_props)
            self._mark_dirty(node)
        if new_layout_node is not None and node.layout_node is not new_layout_node:
            node.layout_node = new_layout_node
            self._mark_dirty(node)
        if new_children is not None:
            self.reconcile_children(node, new_children)

    def reconcile_children(
        self,
        parent: ComponentNode,
        new_children: Sequence[ComponentNode],
    ) -> None:
        """Reconcile a parent's children using key+type matching when possible."""
        existing_children = list(parent.children or [])
        existing_keyed: dict[tuple[str, str], ComponentNode] = {}
        existing_unkeyed: list[ComponentNode] = []
        for child in existing_children:
            if child.key is not None:
                existing_keyed[(child.key, child.type)] = child
            else:
                existing_unkeyed.append(child)

        matched_ids: set[int] = set()
        unkeyed_index = 0
        reconciled: list[ComponentNode] = []

        for incoming in new_children:
            matched: Optional[ComponentNode] = None
            if incoming.key is not None:
                matched = existing_keyed.get((incoming.key, incoming.type))
            else:
                while unkeyed_index < len(existing_unkeyed):
                    candidate = existing_unkeyed[unkeyed_index]
                    unkeyed_index += 1
                    if candidate.type == incoming.type:
                        matched = candidate
                        break

            if matched is None:
                self._attach_subtree(incoming, parent=parent, invoke_mount=True)
                reconciled.append(incoming)
                continue

            matched_ids.add(id(matched))
            matched.parent = parent
            matched.layout_node = incoming.layout_node or matched.layout_node
            matched.event_handlers = {
                name: list(listeners)
                for name, listeners in incoming.event_handlers.items()
            }
            self.update(matched, incoming.props, None)
            self.reconcile_children(matched, incoming.children)
            reconciled.append(matched)

        for child in existing_children:
            if id(child) in matched_ids:
                continue
            self._detach_child(parent, child)

        if _child_identities(existing_children) != _child_identities(reconciled):
            self._mark_dirty(parent)
        parent.children = reconciled

    def unmount(self, node: ComponentNode) -> None:
        """Unmount a node from the tree."""
        if node.parent and node in node.parent.children:
            node.parent.children.remove(node)
            self._mark_dirty(node.parent)
        if self._root is node:
            self._root = None
        self._unmount_subtree(node)

    def flush(self) -> Optional[ComponentNode]:
        """Flush all pending updates and return the root."""
        if self._root:
            self._process_node(self._root)
        self._dirty_nodes.clear()
        self._intrinsic_dirty_nodes.clear()
        return self._root

    def dirty_subtrees(self) -> tuple[ComponentNode, ...]:
        """Return the current dirty roots before flush."""
        dirty_roots: list[ComponentNode] = []
        for node in self._dirty_nodes:
            current = node.parent
            while current is not None and current not in self._dirty_nodes:
                current = current.parent
            if current is None:
                dirty_roots.append(node)
        return tuple(
            sorted(dirty_roots, key=lambda item: (_node_depth(item), item.type, item.key or ""))
        )

    def dirty_layout_nodes(self) -> tuple[LayoutNode, ...]:
        """Return layout nodes on dirty paths for renderer invalidation.

        Intrinsically dirty component nodes are promoted to their layout-node
        ancestry so the renderer can rerender only the affected branch while
        continuing to reuse clean sibling subtree caches.
        """

        ordered: list[LayoutNode] = []
        seen: set[int] = set()

        for node in sorted(
            self._intrinsic_dirty_nodes,
            key=lambda item: (_node_depth(item), item.type, item.key or ""),
        ):
            current: Optional[ComponentNode] = node
            while current is not None:
                layout_node = current.layout_node
                if layout_node is not None and id(layout_node) not in seen:
                    ordered.append(layout_node)
                    seen.add(id(layout_node))
                current = current.parent

        return tuple(ordered)

    def get_root(self) -> Optional[ComponentNode]:
        """Get the root node."""
        return self._root

    def _attach_subtree(
        self,
        node: ComponentNode,
        *,
        parent: Optional[ComponentNode],
        invoke_mount: bool,
    ) -> None:
        node.parent = parent
        node.mounted = True
        node.dirty = True
        self._mark_dirty(node)
        if invoke_mount:
            _call_if_callable(node.props.get("on_mount"), node)
        for child in node.children or []:
            self._attach_subtree(child, parent=node, invoke_mount=invoke_mount)

    def _detach_child(self, parent: ComponentNode, child: ComponentNode) -> None:
        if child in parent.children:
            parent.children.remove(child)
        self._unmount_subtree(child)
        self._mark_dirty(parent)

    def _unmount_subtree(self, node: ComponentNode) -> None:
        for child in list(node.children or []):
            self._unmount_subtree(child)
        _call_if_callable(node.props.get("on_unmount"), node)
        node.children = []
        node.parent = None
        node.mounted = False
        node.dirty = False
        self._dirty_nodes.discard(node)
        self._intrinsic_dirty_nodes.discard(node)

    def _mark_dirty(self, node: ComponentNode, *, intrinsic: bool = True) -> None:
        """Mark a node and its ancestors as dirty."""
        if intrinsic:
            self._intrinsic_dirty_nodes.add(node)
        current: Optional[ComponentNode] = node
        while current:
            current.dirty = True
            self._dirty_nodes.add(current)
            current = current.parent

    def _process_node(self, node: ComponentNode) -> None:
        """Process a node and its children."""
        if node.dirty:
            node.dirty = False
        for child in node.children or []:
            self._process_node(child)


def _child_identities(children: Iterable[ComponentNode]) -> tuple[tuple[str, str | None], ...]:
    return tuple((child.type, child.key) for child in children)


def _node_depth(node: ComponentNode) -> int:
    depth = 0
    current = node.parent
    while current is not None:
        depth += 1
        current = current.parent
    return depth


def create_reconciler() -> Reconciler:
    """Factory function to create a reconciler."""
    return Reconciler()
