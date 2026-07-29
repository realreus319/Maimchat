"""In-process linked transport pair for local MCP communication."""

from __future__ import annotations

import asyncio
from typing import Any, Callable


class InProcessTransport:
    """Minimal in-process transport compatible with request/response cycles."""

    def __init__(self) -> None:
        self._peer: InProcessTransport | None = None
        self._closed = False
        self.onclose: Callable[[], None] | None = None
        self.onerror: Callable[[Exception], None] | None = None
        self.onmessage: Callable[[Any], None] | None = None

    def _set_peer(self, peer: "InProcessTransport") -> None:
        self._peer = peer

    async def start(self) -> None:
        return None

    async def send(self, message: Any) -> None:
        if self._closed:
            raise RuntimeError("Transport is closed")

        loop = asyncio.get_running_loop()
        peer = self._peer

        def _deliver() -> None:
            if peer is None or peer._closed:
                return
            try:
                if peer.onmessage is not None:
                    peer.onmessage(message)
            except Exception as exc:  # pragma: no cover - defensive mirror
                if peer.onerror is not None:
                    peer.onerror(exc)

        loop.call_soon(_deliver)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.onclose is not None:
            self.onclose()
        peer = self._peer
        if peer is not None and not peer._closed:
            peer._closed = True
            if peer.onclose is not None:
                peer.onclose()


def create_linked_transport_pair() -> tuple[InProcessTransport, InProcessTransport]:
    a = InProcessTransport()
    b = InProcessTransport()
    a._set_peer(b)
    b._set_peer(a)
    return a, b
