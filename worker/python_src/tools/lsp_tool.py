from __future__ import annotations

import atexit
import asyncio
import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

try:
    from lsprotocol import types as lsp_types
    from pygls.client import JsonRPCClient as _PyglsJsonRpcClient
except ImportError:  # pragma: no cover - optional dependency
    lsp_types = None
    _PyglsJsonRpcClient = None


MAX_LSP_FILE_SIZE_BYTES = 10_000_000
_SUPPORTED_OPERATIONS = frozenset(
    {
        "documentSymbol",
        "findReferences",
        "goToDefinition",
        "goToImplementation",
        "hover",
        "incomingCalls",
        "outgoingCalls",
        "prepareCallHierarchy",
        "workspaceSymbol",
    }
)
_PYTHON_EXTENSIONS = {".py", ".pyi"}
_TYPESCRIPT_EXTENSIONS = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
_SYMBOL_KIND_NAMES = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}
_SESSION_CACHE_LOCK = threading.Lock()
_SESSION_CACHE: dict[tuple[tuple[str, ...], str], "_PersistentLspSession"] = {}
_PYGLS_SESSION_CACHE: dict[tuple[tuple[str, ...], str], "_PyglsSession"] = {}
_PYGLS_SESSION_LOOP_ID: int | None = None
_JSON_PRIMITIVE_TYPES = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class _LspRequest:
    operation: str
    file_path: Path
    line: int
    character: int


class _JsonRpcClient:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._next_id = 0

    async def request(self, method: str, params: object) -> Any:
        validated_method = _validate_json_rpc_method(method)
        validated_params = _validate_json_rpc_params(
            params,
            context=f"{validated_method} params",
        )
        request_id = self._next_id
        self._next_id += 1
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": validated_method,
                "params": validated_params,
            }
        )
        while True:
            payload = await self._read()
            if payload is None:
                raise RuntimeError("LSP server exited before replying")
            if payload.get("id") != request_id:
                continue
            if payload.get("error") is not None:
                error = payload["error"]
                if isinstance(error, Mapping):
                    message = error.get("message")
                    if isinstance(message, str) and message.strip():
                        raise RuntimeError(message)
                raise RuntimeError("LSP request failed")
            return payload.get("result")

    async def notify(self, method: str, params: object) -> None:
        validated_method = _validate_json_rpc_method(method)
        validated_params = _validate_json_rpc_params(
            params,
            context=f"{validated_method} params",
        )
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": validated_method,
                "params": validated_params,
            }
        )

    async def close(self) -> None:
        try:
            await self.request("shutdown", None)
        except Exception:
            pass
        try:
            await self.notify("exit", None)
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()

    async def _send(self, payload: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            raise RuntimeError("LSP server stdin is unavailable")
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + encoded)
        await self._process.stdin.drain()

    async def _read(self) -> dict[str, Any] | None:
        if self._process.stdout is None:
            raise RuntimeError("LSP server stdout is unavailable")
        headers: dict[str, str] = {}
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return None
            if line in {b"\n", b"\r\n"}:
                break
            key, _, value = line.decode("ascii", errors="replace").partition(":")
            headers[key.strip().lower()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        payload = await self._process.stdout.readexactly(content_length)
        decoded = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(decoded, dict):
            raise RuntimeError("LSP server returned a non-object payload")
        return decoded


class _SyncJsonRpcClient:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._next_id = 0

    def request(self, method: str, params: object) -> Any:
        validated_method = _validate_json_rpc_method(method)
        validated_params = _validate_json_rpc_params(
            params,
            context=f"{validated_method} params",
        )
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": validated_method,
                "params": validated_params,
            }
        )
        while True:
            payload = self._read()
            if payload is None:
                raise RuntimeError("LSP server exited before replying")
            if payload.get("id") != request_id:
                continue
            if payload.get("error") is not None:
                error = payload["error"]
                if isinstance(error, Mapping):
                    message = error.get("message")
                    if isinstance(message, str) and message.strip():
                        raise RuntimeError(message)
                raise RuntimeError("LSP request failed")
            return payload.get("result")

    def notify(self, method: str, params: object) -> None:
        validated_method = _validate_json_rpc_method(method)
        validated_params = _validate_json_rpc_params(
            params,
            context=f"{validated_method} params",
        )
        self._send(
            {
                "jsonrpc": "2.0",
                "method": validated_method,
                "params": validated_params,
            }
        )

    def close(self) -> None:
        try:
            self.request("shutdown", None)
        except Exception:
            pass
        try:
            self.notify("exit", None)
        except Exception:
            pass
        try:
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2.0)

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            raise RuntimeError("LSP server stdin is unavailable")
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + encoded)
        self._process.stdin.flush()

    def _read(self) -> dict[str, Any] | None:
        if self._process.stdout is None:
            raise RuntimeError("LSP server stdout is unavailable")
        headers: dict[str, str] = {}
        while True:
            line = self._process.stdout.readline()
            if not line:
                return None
            if line in {b"\n", b"\r\n"}:
                break
            key, _, value = line.decode("ascii", errors="replace").partition(":")
            headers[key.strip().lower()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        payload = self._process.stdout.read(content_length)
        decoded = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(decoded, dict):
            raise RuntimeError("LSP server returned a non-object payload")
        return decoded


class _PersistentLspSession:
    def __init__(self, *, command: Sequence[str], cwd: Path) -> None:
        self._command = tuple(command)
        self._cwd = cwd
        self._lock = threading.RLock()
        self._client: _SyncJsonRpcClient | None = None
        self._initialized = False
        self._document_versions: dict[str, int] = {}
        self._document_sources: dict[str, str] = {}

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._initialized = False
            self._document_versions.clear()
            self._document_sources.clear()
        if client is not None:
            client.close()

    def execute(self, request: _LspRequest, source: str) -> Any:
        with self._lock:
            client = self._ensure_client()
            try:
                self._ensure_initialized(client)
                self._sync_document(client, request.file_path, source)
                method, params = _method_and_params(request)
                result = client.request(method, params)
                if request.operation in {"incomingCalls", "outgoingCalls"}:
                    call_items = result if isinstance(result, list) else []
                    if not call_items:
                        return []
                    result = client.request(
                        (
                            "callHierarchy/incomingCalls"
                            if request.operation == "incomingCalls"
                            else "callHierarchy/outgoingCalls"
                        ),
                        {"item": call_items[0]},
                    )
                return result
            except Exception:
                self._reset_after_failure()
                raise

    def sync_saved_document(self, file_path: Path, source: str) -> None:
        with self._lock:
            client = self._client
            if client is None or client._process.poll() is not None:
                return
            try:
                self._ensure_initialized(client)
                self._sync_document(client, file_path, source)
                client.notify(
                    "textDocument/didSave",
                    {"textDocument": {"uri": file_path.as_uri()}},
                )
            except Exception:
                self._reset_after_failure()
                raise

    def _ensure_client(self) -> _SyncJsonRpcClient:
        if self._client is not None:
            process = self._client._process
            if process.poll() is None:
                return self._client
        process = subprocess.Popen(
            self._command,
            cwd=str(self._cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._client = _SyncJsonRpcClient(process)
        self._initialized = False
        return self._client

    def _ensure_initialized(self, client: _SyncJsonRpcClient) -> None:
        if self._initialized:
            return
        client.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self._cwd.as_uri(),
                "capabilities": {},
            },
        )
        client.notify("initialized", {})
        self._initialized = True

    def _sync_document(
        self,
        client: _SyncJsonRpcClient,
        file_path: Path,
        source: str,
    ) -> None:
        uri = file_path.as_uri()
        current_version = self._document_versions.get(uri)
        previous_source = self._document_sources.get(uri)
        if current_version is None:
            version = 1
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": _language_id_for_path(file_path),
                        "version": version,
                        "text": source,
                    }
                },
            )
            self._document_versions[uri] = version
            self._document_sources[uri] = source
            return
        if previous_source == source:
            return
        version = current_version + 1
        client.notify(
            "textDocument/didChange",
            {
                "textDocument": {
                    "uri": uri,
                    "version": version,
                },
                "contentChanges": [{"text": source}],
            },
        )
        self._document_versions[uri] = version
        self._document_sources[uri] = source

    def _reset_after_failure(self) -> None:
        client = self._client
        self._client = None
        self._initialized = False
        self._document_versions.clear()
        self._document_sources.clear()
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


async def lsp_call(
    *,
    operation: str,
    file_path: str,
    line: int,
    character: int,
    cwd: str | None = None,
) -> dict[str, Any]:
    request = _validate_request(
        operation=operation,
        file_path=file_path,
        line=line,
        character=character,
    )
    file_size = request.file_path.stat().st_size
    if file_size > MAX_LSP_FILE_SIZE_BYTES:
        return _build_output(
            request,
            result=(
                f"File too large for LSP analysis "
                f"({max(1, round(file_size / 1_000_000))}MB exceeds 10MB limit)"
            ),
        )

    command = _server_command_for_path(request.file_path)
    if command is None:
        return _build_output(
            request,
            result=f"No LSP server available for file type: {request.file_path.suffix}",
        )

    cwd = Path(cwd or os.getcwd()).resolve()
    source = request.file_path.read_text(encoding="utf-8")
    try:
        result = await _execute_lsp_request(
            request=request,
            command=command,
            cwd=cwd,
            source=source,
        )
        formatted_result, result_count, file_count = await _format_result(
            request,
            result,
            cwd,
        )
        return _build_output(
            request,
            result=formatted_result,
            result_count=result_count,
            file_count=file_count,
        )
    except FileNotFoundError:
        return _build_output(
            request,
            result=f"No LSP server available for file type: {request.file_path.suffix}",
        )
    except Exception as exc:
        return _build_output(
            request,
            result=f"Error performing {request.operation}: {exc}",
        )


async def _execute_lsp_request(
    *,
    request: _LspRequest,
    command: Sequence[str],
    cwd: Path,
    source: str,
) -> Any:
    try:
        return await _execute_lsp_request_with_persistent_session(
            request=request,
            command=command,
            cwd=cwd,
            source=source,
        )
    except Exception:
        if _PyglsJsonRpcClient is not None and lsp_types is not None:
            try:
                return await _execute_lsp_request_with_pygls(
                    request=request,
                    command=command,
                    cwd=cwd,
                    source=source,
                )
            except Exception:
                pass
        raise


class _PyglsSession:
    def __init__(self, *, command: Sequence[str], cwd: Path) -> None:
        self._command = tuple(command)
        self._cwd = cwd
        self._client: Any = None
        self._initialized = False
        self._document_versions: dict[str, int] = {}
        self._document_sources: dict[str, str] = {}

    async def execute(self, request: _LspRequest, source: str) -> Any:
        if _PyglsJsonRpcClient is None or lsp_types is None:
            raise RuntimeError("pygls is unavailable")
        try:
            client = await self._ensure_client()
            protocol = client.protocol
            await self._ensure_initialized(protocol)
            await self._sync_document(protocol, request.file_path, source)
            method, params = _typed_method_and_params(request)
            result = await asyncio.wrap_future(protocol.send_request(method, params))
            if request.operation in {"incomingCalls", "outgoingCalls"}:
                call_items = result if isinstance(result, list) else []
                if not call_items:
                    return []
                result = await asyncio.wrap_future(
                    protocol.send_request(
                        (
                            "callHierarchy/incomingCalls"
                            if request.operation == "incomingCalls"
                            else "callHierarchy/outgoingCalls"
                        ),
                        {"item": call_items[0]},
                    )
                )
            return result
        except Exception:
            await self._reset_after_failure()
            raise

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            server = getattr(self._client, "_server", None)
            if server is None or getattr(server, "returncode", None) is None:
                return self._client
            await self._close_client()
        client = _PyglsJsonRpcClient()
        await client.start_io(self._command[0], *self._command[1:], cwd=str(self._cwd))
        self._client = client
        self._initialized = False
        return client

    async def _ensure_initialized(self, protocol: Any) -> None:
        if self._initialized:
            return
        initialize_params = lsp_types.InitializeParams(
            process_id=os.getpid(),
            root_uri=self._cwd.as_uri(),
            capabilities=lsp_types.ClientCapabilities(),
        )
        await asyncio.wrap_future(
            protocol.send_request("initialize", initialize_params)
        )
        protocol.notify("initialized", lsp_types.InitializedParams())
        self._initialized = True

    async def _sync_document(
        self, protocol: Any, file_path: Path, source: str
    ) -> None:
        uri = file_path.as_uri()
        current_version = self._document_versions.get(uri)
        previous_source = self._document_sources.get(uri)
        if current_version is None:
            version = 1
            protocol.notify(
                "textDocument/didOpen",
                lsp_types.DidOpenTextDocumentParams(
                    text_document=lsp_types.TextDocumentItem(
                        uri=uri,
                        language_id=_language_id_for_path(file_path),
                        version=version,
                        text=source,
                    )
                ),
            )
            self._document_versions[uri] = version
            self._document_sources[uri] = source
            return
        if previous_source == source:
            return
        version = current_version + 1
        protocol.notify(
            "textDocument/didChange",
            lsp_types.DidChangeTextDocumentParams(
                text_document=lsp_types.VersionedTextDocumentIdentifier(
                    uri=uri,
                    version=version,
                ),
                content_changes=[lsp_types.TextDocumentContentChangeEvent(text=source)],
            ),
        )
        self._document_versions[uri] = version
        self._document_sources[uri] = source

    async def _reset_after_failure(self) -> None:
        client = self._client
        self._client = None
        self._initialized = False
        self._document_versions.clear()
        self._document_sources.clear()
        if client is not None:
            await self._close_client()

    async def _close_client(self) -> None:
        client = self._client
        if client is None:
            return
        protocol = getattr(client, "protocol", None)
        if protocol is not None:
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(protocol.send_request("shutdown")),
                    timeout=2.0,
                )
            except Exception:
                pass
            try:
                protocol.notify("exit")
            except Exception:
                pass
        try:
            await asyncio.wait_for(client.stop(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        server = getattr(client, "_server", None)
        if server is not None and getattr(server, "returncode", None) is None:
            server.kill()
            try:
                await server.wait()
            except Exception:
                pass
        tasks = tuple(getattr(client, "_async_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _get_pygls_session(
    *,
    command: Sequence[str],
    cwd: Path,
) -> _PyglsSession:
    global _PYGLS_SESSION_LOOP_ID
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None
    if loop_id != _PYGLS_SESSION_LOOP_ID:
        for session in _PYGLS_SESSION_CACHE.values():
            client = session._client
            if client is not None:
                server = getattr(client, "_server", None)
                if server is not None and getattr(server, "returncode", None) is None:
                    try:
                        server.kill()
                    except Exception:
                        pass
        _PYGLS_SESSION_CACHE.clear()
        _PYGLS_SESSION_LOOP_ID = loop_id
    key = (tuple(command), str(cwd.resolve()))
    session = _PYGLS_SESSION_CACHE.get(key)
    if session is None:
        session = _PyglsSession(command=command, cwd=cwd)
        _PYGLS_SESSION_CACHE[key] = session
    return session


async def _execute_lsp_request_with_pygls(
    *,
    request: _LspRequest,
    command: Sequence[str],
    cwd: Path,
    source: str,
) -> Any:
    session = _get_pygls_session(command=command, cwd=cwd)
    return await session.execute(request, source)


async def _close_pygls_client(client: Any) -> None:
    protocol = getattr(client, "protocol", None)
    if protocol is not None:
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(protocol.send_request("shutdown")),
                timeout=2.0,
            )
        except Exception:
            pass
        try:
            protocol.notify("exit")
        except Exception:
            pass
    try:
        await asyncio.wait_for(client.stop(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        pass
    except Exception:
        return

    server = getattr(client, "_server", None)
    if server is not None and getattr(server, "returncode", None) is None:
        server.kill()
        try:
            await server.wait()
        except Exception:
            pass
    tasks = tuple(getattr(client, "_async_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _execute_lsp_request_with_raw_jsonrpc(
    *,
    request: _LspRequest,
    command: Sequence[str],
    cwd: Path,
    source: str,
) -> Any:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = _JsonRpcClient(process)
    try:
        await client.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": cwd.as_uri(),
                "capabilities": {},
            },
        )
        await client.notify("initialized", {})
        await client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": request.file_path.as_uri(),
                    "languageId": _language_id_for_path(request.file_path),
                    "version": 1,
                    "text": source,
                }
            },
        )
        method, params = _method_and_params(request)
        result = await client.request(method, params)
        if request.operation in {"incomingCalls", "outgoingCalls"}:
            call_items = result if isinstance(result, list) else []
            if not call_items:
                return []
            result = await client.request(
                (
                    "callHierarchy/incomingCalls"
                    if request.operation == "incomingCalls"
                    else "callHierarchy/outgoingCalls"
                ),
                {"item": call_items[0]},
            )
        return result
    finally:
        await client.close()


async def _execute_lsp_request_with_persistent_session(
    *,
    request: _LspRequest,
    command: Sequence[str],
    cwd: Path,
    source: str,
) -> Any:
    session = _get_persistent_lsp_session(command=command, cwd=cwd)
    return await asyncio.to_thread(session.execute, request, source)


def _get_persistent_lsp_session(
    *,
    command: Sequence[str],
    cwd: Path,
) -> _PersistentLspSession:
    key = (tuple(command), str(cwd.resolve()))
    with _SESSION_CACHE_LOCK:
        session = _SESSION_CACHE.get(key)
        if session is None:
            session = _PersistentLspSession(command=command, cwd=cwd)
            _SESSION_CACHE[key] = session
        return session


def notify_persistent_lsp_document_saved(
    file_path: str | Path,
    source: str,
    *,
    cwd: str | Path | None = None,
) -> None:
    path = Path(file_path).expanduser().resolve()
    command = _server_command_for_path(path)
    if command is None:
        return
    normalized_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    key = (tuple(command), str(normalized_cwd))
    with _SESSION_CACHE_LOCK:
        session = _SESSION_CACHE.get(key)
    if session is None:
        return
    session.sync_saved_document(path, source)


def close_persistent_lsp_sessions() -> None:
    with _SESSION_CACHE_LOCK:
        sessions = tuple(_SESSION_CACHE.values())
        _SESSION_CACHE.clear()
        pygls_sessions = tuple(_PYGLS_SESSION_CACHE.values())
        _PYGLS_SESSION_CACHE.clear()
    for session in sessions:
        try:
            session.close()
        except Exception:
            continue
    for pygls_session in pygls_sessions:
        client = pygls_session._client
        if client is not None:
            server = getattr(client, "_server", None)
            if server is not None and getattr(server, "returncode", None) is None:
                try:
                    server.kill()
                except Exception:
                    pass


async def aclose_persistent_lsp_sessions() -> None:
    await asyncio.to_thread(close_persistent_lsp_sessions)


def _validate_request(
    *,
    operation: str,
    file_path: str,
    line: int,
    character: int,
) -> _LspRequest:
    if operation not in _SUPPORTED_OPERATIONS:
        raise ValueError(f"Unsupported LSP operation: {operation}")
    absolute_path = Path(file_path).expanduser().resolve()
    if not absolute_path.exists():
        raise ValueError(f"File does not exist: {file_path}")
    if not absolute_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")
    if line < 1:
        raise ValueError("line must be >= 1")
    if character < 0:
        raise ValueError("character must be >= 0")
    return _LspRequest(
        operation=operation,
        file_path=absolute_path,
        line=line,
        character=character,
    )


def _server_command_for_path(path: Path) -> tuple[str, ...] | None:
    suffix = path.suffix.lower()
    if suffix in _PYTHON_EXTENSIONS:
        return _parse_command(
            os.environ.get("PYTHON_LSP_SERVER_COMMAND") or "pyright-langserver --stdio"
        )
    if suffix in _TYPESCRIPT_EXTENSIONS:
        return _parse_command(
            os.environ.get("TYPESCRIPT_LSP_SERVER_COMMAND")
            or "typescript-language-server --stdio"
        )
    return None


def _parse_command(command: str) -> tuple[str, ...]:
    parts = tuple(part for part in shlex.split(command) if part)
    if not parts:
        raise ValueError("LSP server command is empty")
    return parts


def _validate_json_rpc_method(method: object) -> str:
    if not isinstance(method, str) or not method.strip():
        raise ValueError("LSP method must be a non-empty string")
    return method


def _validate_json_rpc_params(params: object, *, context: str = "params") -> object:
    if isinstance(params, _JSON_PRIMITIVE_TYPES):
        return params
    if isinstance(params, Mapping):
        normalized: dict[str, object] = {}
        for key, value in params.items():
            if not isinstance(key, str):
                raise ValueError(f"{context} object keys must be strings")
            normalized[key] = _validate_json_rpc_params(
                value,
                context=f"{context}.{key}",
            )
        return normalized
    if isinstance(params, Sequence) and not isinstance(
        params,
        (str, bytes, bytearray),
    ):
        return [
            _validate_json_rpc_params(value, context=f"{context}[{index}]")
            for index, value in enumerate(params)
        ]
    raise ValueError(f"{context} must be JSON-serializable")


def _language_id_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyi"}:
        return "python"
    if suffix in {".tsx", ".jsx"}:
        return "typescriptreact" if suffix == ".tsx" else "javascriptreact"
    if suffix in {".ts", ".mts", ".cts"}:
        return "typescript"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "javascript"
    return "plaintext"


def _method_and_params(request: _LspRequest) -> tuple[str, dict[str, Any]]:
    uri = request.file_path.as_uri()
    position = {
        "line": request.line - 1,
        "character": max(request.character - 1, 0),
    }
    text_document = {"uri": uri}
    if request.operation == "goToDefinition":
        return "textDocument/definition", {
            "textDocument": text_document,
            "position": position,
        }
    if request.operation == "findReferences":
        return "textDocument/references", {
            "textDocument": text_document,
            "position": position,
            "context": {"includeDeclaration": True},
        }
    if request.operation == "hover":
        return "textDocument/hover", {
            "textDocument": text_document,
            "position": position,
        }
    if request.operation == "documentSymbol":
        return "textDocument/documentSymbol", {"textDocument": text_document}
    if request.operation == "workspaceSymbol":
        return "workspace/symbol", {"query": ""}
    if request.operation == "goToImplementation":
        return "textDocument/implementation", {
            "textDocument": text_document,
            "position": position,
        }
    if request.operation in {
        "prepareCallHierarchy",
        "incomingCalls",
        "outgoingCalls",
    }:
        return "textDocument/prepareCallHierarchy", {
            "textDocument": text_document,
            "position": position,
        }
    raise ValueError(f"Unsupported LSP operation: {request.operation}")


def _typed_method_and_params(request: _LspRequest) -> tuple[str, Any]:
    if lsp_types is None:
        raise RuntimeError("lsprotocol is unavailable")

    text_document = lsp_types.TextDocumentIdentifier(uri=request.file_path.as_uri())
    position = lsp_types.Position(
        line=request.line - 1,
        character=max(request.character - 1, 0),
    )
    if request.operation == "goToDefinition":
        return "textDocument/definition", lsp_types.DefinitionParams(
            text_document=text_document,
            position=position,
        )
    if request.operation == "findReferences":
        return "textDocument/references", lsp_types.ReferenceParams(
            text_document=text_document,
            position=position,
            context=lsp_types.ReferenceContext(include_declaration=True),
        )
    if request.operation == "hover":
        return "textDocument/hover", lsp_types.HoverParams(
            text_document=text_document,
            position=position,
        )
    if request.operation == "documentSymbol":
        return "textDocument/documentSymbol", lsp_types.DocumentSymbolParams(
            text_document=text_document
        )
    if request.operation == "workspaceSymbol":
        return "workspace/symbol", lsp_types.WorkspaceSymbolParams(query="")
    if request.operation == "goToImplementation":
        return "textDocument/implementation", lsp_types.ImplementationParams(
            text_document=text_document,
            position=position,
        )
    if request.operation in {
        "prepareCallHierarchy",
        "incomingCalls",
        "outgoingCalls",
    }:
        return "textDocument/prepareCallHierarchy", lsp_types.CallHierarchyPrepareParams(
            text_document=text_document,
            position=position,
        )
    raise ValueError(f"Unsupported LSP operation: {request.operation}")


async def _format_result(
    request: _LspRequest,
    result: Any,
    cwd: Path,
) -> tuple[str, int | None, int | None]:
    if request.operation in {
        "findReferences",
        "goToDefinition",
        "goToImplementation",
    }:
        return _format_location_result(result, cwd)
    if request.operation == "hover":
        return _format_hover_result(result), None, None
    if request.operation == "documentSymbol":
        return _format_document_symbol_result(result, request.file_path, cwd)
    if request.operation == "workspaceSymbol":
        return await _format_workspace_symbol_result(result, cwd)
    if request.operation == "prepareCallHierarchy":
        return _format_prepare_call_hierarchy_result(result, cwd)
    if request.operation in {"incomingCalls", "outgoingCalls"}:
        return _format_call_hierarchy_result(result, cwd, request.operation)
    return json.dumps(result, ensure_ascii=False, indent=2), None, None


def _format_hover_result(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "No hover information available"
    contents = result.get("contents")
    rendered = _render_hover_contents(contents)
    return rendered or "No hover information available"


def _render_hover_contents(contents: Any) -> str:
    if isinstance(contents, str):
        return contents.strip()
    if isinstance(contents, Mapping):
        value = contents.get("value")
        if isinstance(value, str):
            return value.strip()
        language = contents.get("language")
        if isinstance(language, str) and isinstance(value, str):
            return f"{language}\n{value.strip()}"
        return ""
    if isinstance(contents, Sequence) and not isinstance(contents, (str, bytes, bytearray)):
        parts = [_render_hover_contents(item) for item in contents]
        return "\n\n".join(part for part in parts if part)
    return ""


def _format_location_result(result: Any, cwd: Path) -> tuple[str, int, int]:
    locations = _normalize_locations(result)
    filtered = _filter_gitignored_paths(locations, cwd)
    if not filtered:
        return "No results found", 0, 0
    lines = [_format_location(location, cwd) for location in filtered]
    file_count = len({location.file_path for location in filtered})
    return "\n".join(lines), len(filtered), file_count


def _format_document_symbol_result(
    result: Any,
    file_path: Path,
    cwd: Path,
) -> tuple[str, int, int]:
    if not isinstance(result, list) or not result:
        return "No symbols found", 0, 0
    lines = _flatten_symbols(result, cwd, default_path=file_path)
    if not lines:
        return "No symbols found", 0, 0
    return "\n".join(lines), len(lines), 1


async def _format_workspace_symbol_result(
    result: Any,
    cwd: Path,
) -> tuple[str, int, int]:
    if not isinstance(result, list) or not result:
        return "No symbols found", 0, 0
    filtered = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        location = _location_from_mapping(item.get("location"))
        if location is None:
            continue
        filtered.append((item, location))
    kept_locations = _filter_gitignored_paths(
        [location for _, location in filtered],
        cwd,
    )
    kept_paths = {location.file_path for location in kept_locations}
    lines: list[str] = []
    for item, location in filtered:
        if location.file_path not in kept_paths:
            continue
        name = str(item.get("name") or "<anonymous>")
        kind = _kind_name(item.get("kind"))
        lines.append(
            f"{name} ({kind}) - {_format_location(location, cwd)}"
        )
    if not lines:
        return "No symbols found", 0, 0
    return "\n".join(lines), len(lines), len(kept_paths)


def _format_prepare_call_hierarchy_result(
    result: Any,
    cwd: Path,
) -> tuple[str, int, int]:
    if not isinstance(result, list) or not result:
        return "No call hierarchy item found at this position", 0, 0
    items: list[str] = []
    locations: list[_NormalizedLocation] = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        location = _location_from_call_item(item)
        if location is None:
            continue
        locations.append(location)
        items.append(f"{item.get('name', '<anonymous>')} - {_format_location(location, cwd)}")
    if not items:
        return "No call hierarchy item found at this position", 0, 0
    return "\n".join(items), len(items), len({location.file_path for location in locations})


def _format_call_hierarchy_result(
    result: Any,
    cwd: Path,
    operation: str,
) -> tuple[str, int, int]:
    if not isinstance(result, list) or not result:
        return "No call hierarchy results found", 0, 0
    relation_key = "from" if operation == "incomingCalls" else "to"
    lines: list[str] = []
    locations: list[_NormalizedLocation] = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        target = item.get(relation_key)
        if not isinstance(target, Mapping):
            continue
        location = _location_from_call_item(target)
        if location is None:
            continue
        locations.append(location)
        name = str(target.get("name") or "<anonymous>")
        lines.append(f"{name} - {_format_location(location, cwd)}")
    if not lines:
        return "No call hierarchy results found", 0, 0
    return "\n".join(lines), len(lines), len({location.file_path for location in locations})


def _flatten_symbols(
    symbols: Sequence[Any],
    cwd: Path,
    *,
    default_path: Path,
    depth: int = 0,
) -> list[str]:
    lines: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, Mapping):
            continue
        name = str(symbol.get("name") or "<anonymous>")
        kind = _kind_name(symbol.get("kind"))
        location = _location_from_symbol(symbol, default_path)
        prefix = "  " * depth
        if location is None:
            lines.append(f"{prefix}{name} ({kind})")
        else:
            lines.append(
                f"{prefix}{name} ({kind}) - {_format_location(location, cwd)}"
            )
        children = symbol.get("children")
        if isinstance(children, list) and children:
            lines.extend(
                _flatten_symbols(children, cwd, default_path=default_path, depth=depth + 1)
            )
    return lines


def _kind_name(raw_kind: Any) -> str:
    if isinstance(raw_kind, int):
        return _SYMBOL_KIND_NAMES.get(raw_kind, f"kind_{raw_kind}")
    return "symbol"


@dataclass(frozen=True)
class _NormalizedLocation:
    file_path: Path
    line: int
    character: int


def _normalize_locations(result: Any) -> list[_NormalizedLocation]:
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    locations: list[_NormalizedLocation] = []
    for item in items:
        location = _location_from_mapping(item)
        if location is not None:
            locations.append(location)
    deduped: dict[tuple[str, int, int], _NormalizedLocation] = {}
    for location in locations:
        key = (str(location.file_path), location.line, location.character)
        deduped[key] = location
    return list(deduped.values())


def _location_from_mapping(item: Any) -> _NormalizedLocation | None:
    if not isinstance(item, Mapping):
        return None
    uri = item.get("uri")
    range_data = item.get("range")
    if not isinstance(uri, str):
        uri = item.get("targetUri")
        range_data = item.get("targetRange")
    if not isinstance(uri, str) or not isinstance(range_data, Mapping):
        return None
    return _location_from_uri_and_range(uri, range_data)


def _location_from_call_item(item: Mapping[str, Any]) -> _NormalizedLocation | None:
    uri = item.get("uri")
    selection_range = item.get("selectionRange") or item.get("range")
    if not isinstance(uri, str) or not isinstance(selection_range, Mapping):
        return None
    return _location_from_uri_and_range(uri, selection_range)


def _location_from_symbol(
    symbol: Mapping[str, Any],
    default_path: Path,
) -> _NormalizedLocation | None:
    location = _location_from_mapping(symbol)
    if location is not None:
        return location
    range_data = symbol.get("selectionRange") or symbol.get("range")
    if not isinstance(range_data, Mapping):
        return None
    start = range_data.get("start")
    if not isinstance(start, Mapping):
        return None
    line = int(start.get("line", 0)) + 1
    character = int(start.get("character", 0)) + 1
    return _NormalizedLocation(default_path, line, character)


def _location_from_uri_and_range(
    uri: str,
    range_data: Mapping[str, Any],
) -> _NormalizedLocation | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    start = range_data.get("start")
    if not isinstance(start, Mapping):
        return None
    file_path = Path(unquote(parsed.path)).resolve()
    return _NormalizedLocation(
        file_path=file_path,
        line=int(start.get("line", 0)) + 1,
        character=int(start.get("character", 0)) + 1,
    )


def _format_location(location: _NormalizedLocation, cwd: Path) -> str:
    return f"{_display_path(location.file_path, cwd)}:{location.line}:{location.character}"


def _display_path(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _filter_gitignored_paths(
    locations: Sequence[_NormalizedLocation],
    cwd: Path,
) -> list[_NormalizedLocation]:
    if not locations:
        return []
    candidates: list[tuple[_NormalizedLocation, str]] = []
    for location in locations:
        try:
            candidate = str(location.file_path.relative_to(cwd))
        except ValueError:
            candidate = str(location.file_path)
        candidates.append((location, candidate))

    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "check-ignore", "--stdin"],
            input="\n".join(candidate for _, candidate in candidates),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return list(locations)

    if completed.returncode not in {0, 1}:
        return list(locations)

    ignored = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    if not ignored:
        return list(locations)
    return [
        location
        for location, candidate in candidates
        if candidate not in ignored
    ]


def _build_output(
    request: _LspRequest,
    *,
    result: str,
    result_count: int | None = None,
    file_count: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation": request.operation,
        "filePath": str(request.file_path),
        "result": result,
    }
    if result_count is not None:
        payload["resultCount"] = result_count
    if file_count is not None:
        payload["fileCount"] = file_count
    return payload


atexit.register(close_persistent_lsp_sessions)


__all__ = [
    "MAX_LSP_FILE_SIZE_BYTES",
    "aclose_persistent_lsp_sessions",
    "close_persistent_lsp_sessions",
    "lsp_call",
    "notify_persistent_lsp_document_saved",
]
