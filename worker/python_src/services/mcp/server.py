"""stdio MCP server for exposing local Claude Code tools.

This mirrors the TS ``mcp serve`` entrypoint: a newline-delimited JSON-RPC
server on stdin/stdout with tool discovery and tool invocation backed by the
same local tool executor used by the query loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any, Mapping

from ...local_tool_executor import LocalToolExecutor
from ...query import (
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    createAssistantMessage,
)
from ...state.app_state_store import get_default_app_state


SERVER_NAME = "claude/tengu"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class MCPServerError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _jsonrpc_result(request_id: object, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _jsonrpc_error(
    request_id: object,
    code: int,
    message: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _decode_line(raw_line: object) -> str:
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace").strip()
    return str(raw_line).strip()


def _write_json_line(output_stream: Any, response: object) -> None:
    text = (
        json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    try:
        output_stream.write(text.encode("utf-8"))
    except TypeError:
        output_stream.write(text)
    output_stream.flush()


def _schema_for_mcp(schema: Mapping[str, Any]) -> dict[str, Any]:
    name = schema.get("name")
    description = schema.get("description")
    if not isinstance(name, str) or not name:
        raise ValueError("Tool schema is missing a name")
    input_schema = schema.get("inputSchema") or schema.get("input_schema")
    if not isinstance(input_schema, Mapping):
        input_schema = {"type": "object", "properties": {}}

    tool: dict[str, Any] = {
        "name": name,
        "description": description if isinstance(description, str) else "",
        "inputSchema": dict(input_schema),
    }
    output_schema = schema.get("outputSchema") or schema.get("output_schema")
    if isinstance(output_schema, Mapping):
        tool["outputSchema"] = dict(output_schema)
    return tool


def _new_executor() -> LocalToolExecutor:
    app_state = get_default_app_state(initial_mode="default")
    app_state.tool_permission_context.update(
        {
            "cwd": os.getcwd(),
            "mode": "default",
            "should_avoid_permission_prompts": True,
        }
    )
    return LocalToolExecutor(app_state=app_state)


class MCPStdioServer:
    def __init__(
        self,
        *,
        executor: LocalToolExecutor | None = None,
        version: str = "0.0.0-python-port",
        debug: bool = False,
        verbose: bool = False,
    ) -> None:
        self.executor = executor or _new_executor()
        self.version = version
        self.debug = debug
        self.verbose = verbose

    def list_tools(self) -> list[dict[str, Any]]:
        return [_schema_for_mcp(schema) for schema in self.executor.get_tool_schemas()]

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not any(tool["name"] == name for tool in self.list_tools()):
            return {
                "content": [{"type": "text", "text": f"Tool {name} not found"}],
                "isError": True,
            }

        tool_use_id = f"mcp-tool-{uuid.uuid4().hex}"
        tool_use = ToolUseBlock(id=tool_use_id, name=name, input=dict(arguments))
        assistant = createAssistantMessage(content=(tool_use,))

        results: list[ToolResultBlock] = []
        rendered_results: list[str] = []
        async for update in self.executor.run((assistant,)):
            message = update.message
            if not isinstance(message, UserMessage):
                continue
            rendered = getattr(message, "toolUseResult", None)
            content = message.message.content
            if not isinstance(content, tuple):
                continue
            for block in content:
                if not isinstance(block, ToolResultBlock):
                    continue
                if block.tool_use_id != tool_use_id:
                    continue
                results.append(block)
                result_text = rendered if rendered is not None else block.content
                rendered_results.append(_json_text(result_text))

        if not results:
            return {
                "content": [{"type": "text", "text": f"Tool produced no result: {name}"}],
                "isError": True,
            }

        is_error = any(block.is_error for block in results)
        text = "\n".join(rendered_results)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return result

    async def handle_message(self, message: object) -> object | None:
        if isinstance(message, list):
            if not message:
                return _jsonrpc_error(
                    None,
                    JSONRPC_INVALID_REQUEST,
                    "Invalid JSON-RPC request",
                )
            responses = [await self.handle_message(item) for item in message]
            return [response for response in responses if response is not None] or None

        if not isinstance(message, Mapping):
            return _jsonrpc_error(
                None,
                JSONRPC_INVALID_REQUEST,
                "Invalid JSON-RPC request",
            )

        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if not has_id:
                return None
            return _jsonrpc_error(
                request_id,
                JSONRPC_INVALID_REQUEST,
                "Invalid JSON-RPC request",
            )

        if method == "notifications/initialized":
            return None

        if not has_id:
            return None

        try:
            result = await self._dispatch(method, message.get("params"))
        except MCPServerError as exc:
            return _jsonrpc_error(request_id, exc.code, exc.message)
        except Exception as exc:
            if self.debug or self.verbose:
                print(f"mcp serve internal error: {exc}", file=sys.stderr)
            return _jsonrpc_error(request_id, JSONRPC_INTERNAL_ERROR, str(exc))
        return _jsonrpc_result(request_id, result)

    async def _dispatch(self, method: str, params: object) -> Mapping[str, Any]:
        if method == "initialize":
            protocol_version = DEFAULT_PROTOCOL_VERSION
            if isinstance(params, Mapping):
                requested_version = params.get("protocolVersion")
                if isinstance(requested_version, str) and requested_version.strip():
                    protocol_version = requested_version
            return {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": self.version},
            }

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": self.list_tools()}

        if method == "tools/call":
            if not isinstance(params, Mapping):
                raise MCPServerError(
                    JSONRPC_INVALID_PARAMS,
                    "tools/call params must be an object",
                )
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name.strip():
                raise MCPServerError(
                    JSONRPC_INVALID_PARAMS,
                    "tools/call params.name must be a non-empty string",
                )
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, Mapping):
                raise MCPServerError(
                    JSONRPC_INVALID_PARAMS,
                    "tools/call params.arguments must be an object",
                )
            return await self.call_tool(name, arguments)

        raise MCPServerError(JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

    async def serve(
        self,
        input_stream: Any,
        output_stream: Any,
    ) -> None:
        for raw_line in input_stream:
            line = _decode_line(raw_line)
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response: object | None = _jsonrpc_error(
                    None,
                    JSONRPC_PARSE_ERROR,
                    f"Parse error: {exc.msg}",
                )
            else:
                response = await self.handle_message(message)
            if response is None:
                continue
            _write_json_line(output_stream, response)


def run_mcp_stdio_server(
    *,
    version: str = "0.0.0-python-port",
    debug: bool = False,
    verbose: bool = False,
) -> int:
    server = MCPStdioServer(version=version, debug=debug, verbose=verbose)
    asyncio.run(server.serve(sys.stdin.buffer, sys.stdout.buffer))
    return 0


__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "MCPStdioServer",
    "SERVER_NAME",
    "run_mcp_stdio_server",
]
