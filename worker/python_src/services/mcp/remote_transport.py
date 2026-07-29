"""HTTP/SSE JSON-RPC transport for remote MCP servers."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Mapping
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request

from ..api.transport import pooled_urlopen


MAX_RECONNECT_ATTEMPTS = 3
INITIAL_RECONNECT_DELAY_S = 0.5
MAX_RECONNECT_DELAY_S = 4.0


class RemoteMCPJsonRpcClient:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        transport_type: str = "http",
        timeout_s: float = 30.0,
    ) -> None:
        self.name = name
        self.url = url
        self.headers = dict(headers or {})
        self.transport_type = transport_type
        self.timeout_s = max(timeout_s, 0.01)
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._sse_message_url: str | None = None
        self._connected = True

    async def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_sync, payload, self.timeout_s)

    async def call_tool(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_ms: int | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        timeout_s = self.timeout_s if timeout_ms is None else max(timeout_ms / 1000, 0.01)
        request_payload = {
            "method": "tools/call",
            "params": dict(payload),
        }
        return await asyncio.to_thread(self._request_sync, request_payload, timeout_s)

    async def notification(self, payload: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self._post_json, self._target_url(), payload, self.timeout_s)

    def close(self) -> None:
        return None

    def _request_sync(
        self,
        payload: Mapping[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        jsonrpc_payload = self._jsonrpc_payload(payload)
        last_error: Exception | None = None
        delay = INITIAL_RECONNECT_DELAY_S

        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            try:
                response = self._post_json(
                    self._target_url(), jsonrpc_payload, timeout_s
                )
                return self._normalize_jsonrpc_response(
                    response, jsonrpc_payload.get("id")
                )
            except (URLError, OSError) as exc:
                last_error = exc
                if attempt < MAX_RECONNECT_ATTEMPTS - 1:
                    self._reconnect()
                    time.sleep(delay)
                    delay = min(delay * 2, MAX_RECONNECT_DELAY_S)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"MCP server '{self.name}' request failed")

    def _jsonrpc_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized.setdefault("jsonrpc", "2.0")
        if "id" not in normalized:
            normalized["id"] = self._next_request_id()
        return normalized

    def _next_request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
        return request_id

    def _reconnect(self) -> None:
        self._connected = True
        if self.transport_type == "sse":
            self._sse_message_url = None

    def _target_url(self) -> str:
        if self.transport_type != "sse":
            return self.url
        if self._sse_message_url is None:
            self._sse_message_url = self._resolve_sse_message_url()
        return self._sse_message_url

    def _resolve_sse_message_url(self) -> str:
        request = Request(
            self.url,
            headers={
                **self.headers,
                "Accept": "text/event-stream",
            },
            method="GET",
        )
        try:
            with pooled_urlopen(request, timeout=self.timeout_s, preload_content=True) as response:
                body = _read_response_bytes(response).decode("utf-8", errors="replace")
        except Exception:
            return self.url

        for event in _parse_sse_events(body):
            event_name = event.get("event")
            data = event.get("data")
            if event_name == "endpoint" and isinstance(data, str) and data.strip():
                return urljoin(self.url, data.strip())
            if isinstance(data, Mapping):
                endpoint = data.get("endpoint")
                if isinstance(endpoint, str) and endpoint.strip():
                    return urljoin(self.url, endpoint.strip())
        return self.url

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_s: float,
    ) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                **self.headers,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with pooled_urlopen(request, timeout=timeout_s, preload_content=True) as response:
            raw = _read_response_bytes(response)
            content_type = ""
            headers = getattr(response, "headers", None)
            getter = getattr(headers, "get", None)
            if callable(getter):
                content_type = str(getter("Content-Type", "") or "")
        text = raw.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type.lower():
            events = _parse_sse_events(text)
            return [event.get("data") for event in events if "data" in event]
        return json.loads(text) if text.strip() else {}

    def _normalize_jsonrpc_response(
        self,
        response: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        candidates = response if isinstance(response, list) else [response]
        first_mapping: Mapping[str, Any] | None = None
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            if first_mapping is None:
                first_mapping = candidate
            if candidate.get("id") == request_id:
                return _jsonrpc_result(candidate)
        if first_mapping is not None:
            return _jsonrpc_result(first_mapping)
        raise RuntimeError(f"MCP server '{self.name}' returned invalid response")


def _jsonrpc_result(response: Mapping[str, Any]) -> dict[str, Any]:
    if "error" in response:
        raise RuntimeError(f"MCP JSON-RPC error: {response['error']!r}")
    result = response.get("result")
    if isinstance(result, Mapping):
        return dict(result)
    if result is None and "jsonrpc" not in response:
        return dict(response)
    return {"content": result}


def _parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if event_name is not None or data_lines:
                events.append(_build_sse_event(event_name, data_lines))
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if event_name is not None or data_lines:
        events.append(_build_sse_event(event_name, data_lines))
    return events


def _build_sse_event(event_name: str | None, data_lines: list[str]) -> dict[str, Any]:
    raw_data = "\n".join(data_lines)
    data: Any = raw_data
    if raw_data:
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            data = raw_data
    event: dict[str, Any] = {}
    if event_name is not None:
        event["event"] = event_name
    if data_lines:
        event["data"] = data
    return event


def _read_response_bytes(response: Any) -> bytes:
    wrapped = getattr(response, "_response", None)
    data = getattr(wrapped, "data", None)
    if isinstance(data, bytes) and data:
        return data
    raw = response.read()
    if isinstance(raw, bytes):
        return raw
    return bytes(raw or b"")


__all__ = ["RemoteMCPJsonRpcClient"]
