package com.l2dchat.core.mem

import com.google.gson.Gson
import com.google.gson.JsonParser
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition

/**
 * Bounded ReAct agent loop for the mem library — Kotlin port of
 * `mem/react.py` (design doc section 1).
 *
 * The loop alternates: LLM call → validate and execute tool calls → feed tool
 * results back → repeat, until the model answers with plain text, the turn
 * budget is exhausted, or validation fails twice in a row.
 *
 * ## Why not reuse [LlmClient.chatCompletionWithTools]?
 *
 * The host's [LlmClient] already offers a tool-calling helper, but its
 * semantics differ from the Python `ReActLoop` in three load-bearing ways:
 * 1. It does not count turns or abort on a budget — the mem loop must enforce
 *    [maxTurns] and raise [ReactLoopAbort] when exhausted.
 * 2. It does not abort on consecutive validation failures — the mem loop must
 *    raise [ReactLoopAbort] after [MAX_CONSECUTIVE_VALIDATION_FAILURES] turns
 *    that each contain at least one validation failure.
 * 3. It does not produce a [ToolCallRecord] audit log — the pipeline's closing
 *    validation is computed from that log (see [ExtractionPipeline]).
 *
 * So this loop drives the lower-level [LlmClient.chatCompletion] itself,
 * passing the tool schemas so the model can request calls, and dispatches each
 * call through the registered [ToolDef.handler]. The accumulated
 * [ToolCallRecord] list is the single source of truth for what the loop did.
 *
 * @property llm Chat client (real or a scripted fake).
 * @property tools Whitelisted tools keyed by name; calls to any other name are
 *   validation failures.
 * @property config Generation config (model, temperature, …) forwarded to
 *   every [LlmClient.chatCompletion] call.
 * @property maxTurns Maximum LLM turns before aborting.
 */
class ReActLoop(
    private val llm: LlmClient,
    private val tools: Map<String, ToolDef>,
    private val config: LlmGenerationConfig,
    private val maxTurns: Int = DEFAULT_MAX_TURNS,
) {
    init {
        require(maxTurns >= 1) { "ReActLoop: maxTurns must be >= 1, got $maxTurns" }
    }

    /**
     * Run the loop to completion or abort.
     *
     * @param systemPrompt System prompt.
     * @param userContent Initial user message content parts (text + image_url
     *   parts for multimodal extraction batches).
     * @return The loop outcome once the model answers with plain text.
     * @throws ReactLoopAbort On exhausting [maxTurns], or on
     *   [MAX_CONSECUTIVE_VALIDATION_FAILURES] consecutive turns containing a
     *   validation failure. Carries the call log.
     */
    suspend fun run(
        systemPrompt: String,
        userContent: List<LlmContentPart>,
    ): LoopResult {
        val messages: MutableList<LlmMessage> =
            ArrayList(2 + maxTurns * 3)
        messages.add(LlmMessage.system(systemPrompt))
        messages.add(LlmMessage(LlmMessageRole.USER, userContent))

        val toolSchemas: List<LlmToolDefinition> = tools.values.map { it.schema }
        val records: MutableList<ToolCallRecord> = ArrayList()
        var consecutiveFailures = 0

        for (turn in 1..maxTurns) {
            val response = llm.chatCompletion(messages, config)
            val result = response.message
            val toolCalls = result.toolCalls

            if (toolCalls.isEmpty()) {
                return LoopResult(
                    finalContent = response.text,
                    calls = records,
                    turns = turn,
                    stopReason = "completed",
                )
            }

            messages.add(result)
            var turnFailed = false
            for (call in toolCalls) {
                val (record, reply) = executeCall(call, turn)
                records.add(record)
                if (!record.validationOk) turnFailed = true
                messages.add(
                    LlmMessage(
                        role = LlmMessageRole.TOOL,
                        content = listOf(LlmTextPart(reply)),
                        toolCallId = call.id,
                        name = call.name,
                    )
                )
            }

            consecutiveFailures = if (turnFailed) consecutiveFailures + 1 else 0
            if (consecutiveFailures >= MAX_CONSECUTIVE_VALIDATION_FAILURES) {
                throw ReactLoopAbort(
                    "aborting after $consecutiveFailures consecutive turns with validation failures",
                    calls = records,
                )
            }
        }

        throw ReactLoopAbort(
            "aborting after reaching max_turns=$maxTurns",
            calls = records,
        )
    }

    /**
     * Validate and execute one tool call.
     *
     * @param call The normalized tool call.
     * @param turn Current 1-based loop turn.
     * @return The audit record and the text to feed back in the tool message
     *   (handler result, or the validation error description).
     */
    private suspend fun executeCall(call: LlmToolCall, turn: Int): Pair<ToolCallRecord, String> {
        val arguments: Any? = parseArguments(call.argumentsJson)
        val record = ToolCallRecord(turn = turn, name = call.name, arguments = arguments)
        val tool = tools[call.name]
        when {
            tool == null -> {
                record.validationOk = false
                record.error = "unknown tool: '${call.name}'"
            }
            arguments !is Map<*, *> -> {
                record.validationOk = false
                record.error =
                    "tool arguments must be a JSON object, got ${arguments?.javaClass?.simpleName ?: "null"}"
            }
            else -> {
                @Suppress("UNCHECKED_CAST")
                val kwargs = arguments as Map<String, Any?>
                try {
                    record.result = tool.handler.invoke(kwargs)
                } catch (e: ToolValidationError) {
                    record.validationOk = false
                    record.error = e.message ?: "tool validation error"
                }
            }
        }
        val reply =
            if (record.validationOk) {
                stringify(record.result)
            } else {
                "tool call rejected: ${record.error}"
            }
        return record to reply
    }

    /**
     * Parse the raw JSON arguments string into a Kotlin value.
     *
     * An empty/blank string is treated as an empty object (matches the
     * [LlmToolCall] default of `"{}"`). A non-object JSON value is returned
     * verbatim so [executeCall] can reject it as a validation failure.
     */
    private fun parseArguments(raw: String): Any? {
        val text = raw.ifBlank { "{}" }
        val element =
            try {
                JsonParser.parseString(text)
            } catch (e: Exception) {
                return text
            }
        return when {
            element.isJsonObject -> GSON.fromJson(element, Map::class.java)
            element.isJsonNull -> null
            else -> text
        }
    }

    /** Render a handler result as tool-message content. Mirrors `_stringify`. */
    private fun stringify(result: Any?): String =
        when (result) {
            is String -> result
            null -> "null"
            else -> GSON.toJson(result)
        }

    companion object {
        /** Default turn budget, matching `react.py`'s implicit default. */
        const val DEFAULT_MAX_TURNS: Int = 12

        /**
         * Consecutive turns with at least one validation failure before the
         * loop aborts. Mirrors `MAX_CONSECUTIVE_VALIDATION_FAILURES = 2`.
         */
        const val MAX_CONSECUTIVE_VALIDATION_FAILURES: Int = 2

        private val GSON: Gson = Gson()
    }
}

/**
 * A tool the loop may call — Kotlin port of `react.py::ToolDef`.
 *
 * @property schema The [LlmToolDefinition] passed to the provider unchanged.
 * @property handler Invoked with the call arguments as a `Map<String, Any?>`.
 *   Raise [ToolValidationError] to signal argument validation failure.
 */
data class ToolDef(
    val schema: LlmToolDefinition,
    val handler: suspend (Map<String, Any?>) -> Any?,
)

/**
 * Audit record for one tool call attempted by the model — Kotlin port of
 * `react.py::ToolCallRecord`.
 *
 * @property turn 1-based loop turn in which the call was made.
 * @property name Requested tool name.
 * @property arguments Parsed call arguments.
 * @property result Handler return value (`null` if not executed).
 * @property validationOk `false` when the call failed validation.
 * @property error Validation/execution error description, if any.
 */
data class ToolCallRecord(
    val turn: Int,
    val name: String,
    val arguments: Any?,
    var result: Any? = null,
    var validationOk: Boolean = true,
    var error: String? = null,
)

/**
 * Outcome of a completed ReAct loop — Kotlin port of `react.py::LoopResult`.
 *
 * @property finalContent The model's closing text.
 * @property calls All tool call records in execution order.
 * @property turns Number of LLM turns used.
 * @property stopReason Why the loop stopped (`"completed"`).
 */
data class LoopResult(
    val finalContent: String?,
    val calls: List<ToolCallRecord> = emptyList(),
    val turns: Int = 0,
    val stopReason: String = "completed",
)
