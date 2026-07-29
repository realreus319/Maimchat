package com.l2dchat.shell

/**
 * IPC contract between the main app and the headless engine package. Mirrored verbatim on the client
 * side (the two are separate APKs and cannot share a class). Messenger-based, same style as the
 * app's existing ChatService.
 */
object ShellProtocol {
    const val ENGINE_PACKAGE = "com.l2dchat.shell"
    const val SERVICE_CLASS = "com.l2dchat.shell.ShellService"
    const val PERMISSION = "com.l2dchat.permission.SHELL_ENGINE"

    // client -> service
    const val MSG_REGISTER = 1
    const val MSG_RUN_TASK = 2
    const val MSG_CANCEL = 3
    const val MSG_BROWSER_RESULT = 4 // app -> service: reverse-bridge result for a browser action
    const val MSG_WARMUP = 5 // app -> service: extract the rootfs now (foreground), reply MSG_READY
    const val MSG_GET_MCP_PORT = 6 // app -> service: ask for the local MCP server port (debug)
    const val MSG_RESULT_ACK = 7 // app -> service: result for taskId persisted/delivered, drop the buffer
    const val MSG_RUN_SESSION = 8 // app -> service: start a PERSISTENT injectable worker session (#5)
    const val MSG_INJECT = 9 // app -> service: inject a user message (KEY_TEXT) into session KEY_TASK_ID
    const val MSG_CLOSE_SESSION = 10 // app -> service: end a persistent session

    // service -> client
    const val MSG_READY = 101
    const val MSG_PROGRESS = 102
    const val MSG_RESULT = 103
    const val MSG_ERROR = 104
    const val MSG_BROWSER_ACTION = 105 // service -> app: reverse-bridge browser tool call
    const val MSG_MCP_PORT = 106 // service -> app: the local MCP server port
    const val MSG_SESSION_ENDED = 107 // service -> app: a persistent session fully ended (unblock caller)

    // bundle keys
    const val KEY_TASK_ID = "taskId"
    const val KEY_TASK = "task"
    const val KEY_API_KEY = "apiKey"
    const val KEY_BASE_URL = "baseUrl"
    const val KEY_MODEL = "model"
    const val KEY_SETTINGS_JSON = "settingsJson" // CC-format settings.json shipped to the worker
    const val KEY_TIMEOUT_MS = "timeoutMs"
    const val KEY_LINE = "line"
    const val KEY_KIND = "kind" // "tool" | "text" | "status"
    const val KEY_EXIT = "exit"
    const val KEY_TEXT = "text"
    const val KEY_STEPS = "steps" // String[] of tool-step summaries
    const val KEY_ERROR = "error"

    // Origin of the delegating turn — echoed through the durable buffer so a result recovered after an
    // app-process kill can be routed back to the right conversation and replayed as a reply.
    const val KEY_ORIGIN_CONTEXT = "originContext"
    const val KEY_ORIGIN_AGENT = "originAgent"
    const val KEY_ORIGIN_MESSAGE = "originMessage"

    // reverse browser bridge
    const val KEY_BROWSER_TOKEN = "browserToken"
    const val KEY_BROWSER_NAME = "browserName"
    const val KEY_BROWSER_ARGS = "browserArgs" // JSON string of the tool arguments
    const val KEY_BROWSER_RESULT_TEXT = "browserResultText"
    const val KEY_BROWSER_IS_ERROR = "browserIsError"
    const val KEY_MCP_PORT = "mcpPort"
}
