package com.l2dchat.core.mem

/**
 * Exception hierarchy for the mem library.
 *
 * Mirrors `mem/errors.py` from the Python reference. All mem-specific errors
 * should derive from [MemError] so callers can catch a single root type.
 */
sealed class MemError(message: String) : Exception(message)

/** Raised when the mem configuration is invalid or incomplete. */
class ConfigError(message: String) : MemError(message)

/**
 * Raised when a vector's dimension does not match the configured dimension.
 */
class DimMismatchError(message: String) : MemError(message)

/**
 * Raised when a tool call fails validation (bad name, bad args, bad fields).
 */
class ToolValidationError(message: String) : MemError(message)

/**
 * Raised when the ReAct loop must abort (max turns or repeated validation failure).
 *
 * @property calls Tool call records accumulated before the abort, for diagnostics.
 *   Snapshotted at construction time so later mutations of the source list do not
 *   leak into this error, matching `list(calls)` in the Python reference.
 */
class ReactLoopAbort(
    message: String,
    calls: List<Any> = emptyList(),
) : MemError(message) {
    val calls: List<Any> = calls.toList()
}

/**
 * Raised when an LLM/embedding provider call fails after all retries.
 */
class ProviderError(message: String) : MemError(message)