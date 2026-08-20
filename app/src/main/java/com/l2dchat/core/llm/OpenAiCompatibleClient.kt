package com.l2dchat.core.llm

import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

class OpenAiCompatibleClient(
        baseUrl: String,
        private val apiKeyProvider: () -> String? = { null },
        private val httpClient: OkHttpClient = OkHttpClient.Builder().build(),
        private val gson: Gson = Gson(),
        private val maxRetries: Int = 5,
        private val retryDelayMillis: Long = 250L,
        private val extraHeaders: Map<String, String> = emptyMap()
) : LlmClient {
    private val chatCompletionsUrl = normalizeChatCompletionsUrl(baseUrl)
    private val timingLogger = com.l2dchat.logging.L2DLogger.module(com.l2dchat.logging.LogModule.CHAT)

    init {
        require(maxRetries >= 0) { "maxRetries must be non-negative" }
        require(retryDelayMillis >= 0) { "retryDelayMillis must be non-negative" }
    }

    override suspend fun chatCompletion(
            messages: List<LlmMessage>,
            config: LlmGenerationConfig
    ): LlmResponse =
            executeWithRetry(config) {
                val body = buildRequestBody(messages, tools = emptyList(), config = config, stream = false)
                val request = buildRequest(body, config)
                parseCompletionResponse(executeRequest(request, config))
            }

    override fun chatCompletionStream(
            messages: List<LlmMessage>,
            config: LlmGenerationConfig
    ): Flow<LlmStreamEvent> =
            flow {
                val body = buildRequestBody(messages, tools = emptyList(), config = config, stream = true)
                val request = buildRequest(body, config)
                streamRequest(request, config).collect { emit(it) }
            }

    override suspend fun chatCompletionWithTools(
            messages: List<LlmMessage>,
            tools: List<LlmToolDefinition>,
            config: LlmGenerationConfig,
            toolExecutor: LlmToolExecutor
    ): LlmResponse {
        require(tools.isNotEmpty()) { "chatCompletionWithTools requires at least one tool" }
        val history = messages.toMutableList()
        var lastResponse: LlmResponse? = null

        repeat(config.maxToolRounds + 1) { round ->
            val roundStart = System.currentTimeMillis()
            val response =
                    streamToCompletion(
                            buildRequest(
                                    buildRequestBody(
                                            messages = history,
                                            tools = tools,
                                            config = config,
                                            stream = true
                                    ),
                                    config
                            ),
                            config
                    )
            timingLogger.info(
                    "[timing] planner LLM round $round took ${System.currentTimeMillis() - roundStart}ms " +
                            "finish=${response.finishReason} " +
                            "toolCalls=[${response.toolCalls.joinToString { it.name }}]"
            )
            lastResponse = response
            if (response.toolCalls.isEmpty()) {
                return response
            }
            if (round >= config.maxToolRounds) {
                // The model still wants tools after exhausting the round budget. Rather than
                // failing the whole turn, run one final completion with tools disabled so the
                // model is forced to produce a usable text answer from the accumulated context.
                return forceFinalCompletion(history, response, config)
            }

            history.add(response.message)
            response.toolCalls.forEach { toolCall ->
                val toolStart = System.currentTimeMillis()
                val result = toolExecutor.execute(toolCall)
                timingLogger.info(
                        "[timing] tool ${toolCall.name} took ${System.currentTimeMillis() - toolStart}ms"
                )
                history.add(LlmMessage.toolResult(result))
                // T5: when a tool result carries multimodal content parts (e.g. an ltm_read_media
                // photo whose bytes were inlined as an image_url), emit a synthetic user message
                // carrying those parts right after the tool message — the OpenAI tool message wire
                // format only supports a string content, so the image cannot ride on it.
                val extraParts = result.contentParts
                if (extraParts != null && extraParts.any { it !is LlmTextPart }) {
                    history.add(
                        LlmMessage(
                            role = LlmMessageRole.USER,
                            content = extraParts,
                        )
                    )
                }
            }
        }

        return lastResponse
                ?: throw OpenAiToolLoopException("LLM tool loop produced no response")
    }

    private suspend fun forceFinalCompletion(
            history: List<LlmMessage>,
            lastToolResponse: LlmResponse,
            config: LlmGenerationConfig
    ): LlmResponse {
        val finalHistory =
                history +
                        LlmMessage.system(
                                "Tool usage budget is exhausted. Reply to the user now with a " +
                                        "final text answer and do not request any more tools."
                        )
        val finalConfig = config.copy(toolChoice = LlmToolChoice.NONE)
        val response =
                streamToCompletion(
                        buildRequest(
                                buildRequestBody(
                                        messages = finalHistory,
                                        tools = emptyList(),
                                        config = finalConfig,
                                        stream = true
                                ),
                                finalConfig
                        ),
                        finalConfig
                )
        return if (response.text.isBlank()) lastToolResponse else response
    }

    /**
     * Drive a non-streaming-style call (tool/decision round) over the STREAMING transport so the
     * first-token vs inter-token idle timeouts apply, but return the COMPLETE accumulated response —
     * the planner needs the full tool_calls + text before it can act. [streamRequest] already retries
     * connection-phase failures (incl. a first-token timeout) internally.
     */
    private suspend fun streamToCompletion(request: Request, config: LlmGenerationConfig): LlmResponse {
        var completed: LlmResponse? = null
        streamRequest(request, config).collect { event ->
            if (event is LlmStreamEvent.Completed) completed = event.response
        }
        return completed ?: throw OpenAiEmptyResponseException()
    }

    private suspend fun <T> executeWithRetry(
            config: LlmGenerationConfig,
            block: suspend () -> T
    ): T {
        var attempt = 0
        var lastError: Throwable? = null
        while (attempt <= maxRetries) {
            try {
                return block()
            } catch (throwable: Throwable) {
                if (throwable is CancellationException) {
                    throw throwable
                }
                lastError = throwable
                if (attempt >= maxRetries || !throwable.isRetryable()) {
                    throw throwable
                }
                delay(retryDelayMillis * (attempt + 1))
                attempt += 1
            }
        }
        throw OpenAiClientException("LLM request failed", lastError)
    }

    private fun Throwable.isRetryable(): Boolean =
            this is IOException ||
                    (this is OpenAiHttpException &&
                            (statusCode == 429 || statusCode in 500..599))

    private fun buildRequestBody(
            messages: List<LlmMessage>,
            tools: List<LlmToolDefinition>,
            config: LlmGenerationConfig,
            stream: Boolean
    ): JsonObject =
            JsonObject().apply {
                addProperty("model", config.model)
                add("messages", messages.toOpenAiMessages())
                addProperty("stream", stream)
                config.temperature?.let { addProperty("temperature", it) }
                config.maxTokens?.let { addProperty("max_tokens", it) }
                // Qwen3/DashScope chain-of-thought toggle. Only sent when explicitly configured so
                // providers that don't recognise the field are unaffected.
                config.enableThinking?.let { addProperty("enable_thinking", it) }
                if (tools.isNotEmpty()) {
                    add("tools", tools.toOpenAiTools())
                    add("tool_choice", config.toolChoice.toOpenAiToolChoice())
                }
            }

    private fun buildRequest(body: JsonObject, config: LlmGenerationConfig): Request {
        val requestBody =
                gson.toJson(body).toRequestBody("application/json; charset=utf-8".toMediaType())
        val builder =
                Request.Builder()
                        .url(chatCompletionsUrl)
                        .post(requestBody)
                        .addHeader("Content-Type", "application/json")
        apiKeyProvider()?.takeIf { it.isNotBlank() }?.let {
            builder.addHeader("Authorization", "Bearer $it")
        }
        extraHeaders.forEach { (name, value) ->
            if (name.isNotBlank() && value.isNotBlank()) {
                builder.addHeader(name, value)
            }
        }
        return builder.build()
    }

    private suspend fun executeRequest(request: Request, config: LlmGenerationConfig): String =
            suspendCancellableCoroutine { continuation ->
                val call = clientFor(config).newCall(request)
                continuation.invokeOnCancellation { call.cancel() }
                call.enqueue(
                        object : Callback {
                            override fun onFailure(call: Call, e: IOException) {
                                if (continuation.isActive) {
                                    continuation.resumeWithException(e)
                                }
                            }

                            override fun onResponse(call: Call, response: Response) {
                                response.use {
                                    val bodyText = it.body?.string().orEmpty()
                                    if (!it.isSuccessful) {
                                        if (continuation.isActive) {
                                            continuation.resumeWithException(
                                                    OpenAiHttpException(
                                                            statusCode = it.code,
                                                            responseBody = bodyText
                                                    )
                                            )
                                        }
                                        return
                                    }
                                    if (bodyText.isBlank()) {
                                        if (continuation.isActive) {
                                            continuation.resumeWithException(
                                                    OpenAiEmptyResponseException()
                                            )
                                        }
                                        return
                                    }
                                    if (continuation.isActive) {
                                        continuation.resume(bodyText)
                                    }
                                }
                            }
                        }
                )
            }

    private fun streamRequest(
            request: Request,
            config: LlmGenerationConfig
    ): Flow<LlmStreamEvent> =
            flow {
                val firstTokenBudgetMillis =
                        minOf(
                                config.timeoutMillis ?: DEFAULT_CALL_TIMEOUT_MILLIS,
                                FIRST_TOKEN_TIMEOUT_MILLIS
                        )
                var attempt = 0
                while (true) {
                    val accumulator = StreamAccumulator(config.model)
                    var emittedAny = false
                    val call = clientFor(config, forStreaming = true).newCall(request)
                    val completionHandle =
                            currentCoroutineContext()[Job]?.invokeOnCompletion { cause ->
                                if (cause is CancellationException) {
                                    call.cancel()
                                }
                            }
                    try {
                        val response = withContext(Dispatchers.IO) { call.execute() }
                        response.use {
                            val body = it.body ?: throw OpenAiEmptyResponseException()
                            if (!it.isSuccessful) {
                                throw OpenAiHttpException(
                                        statusCode = it.code,
                                        responseBody = body.string()
                                )
                            }
                            body.charStream().buffered().use { reader ->
                                while (true) {
                                    currentCoroutineContext().ensureActive()
                                    // Phase 2 (no token yet) vs Phase 3 (streaming): distinct budgets so
                                    // long first-token "thinking" is never misread as an inter-token
                                    // stall (the old conflation bug).
                                    val readBudgetMillis =
                                            if (emittedAny) STREAM_IDLE_TIMEOUT_MILLIS
                                            else firstTokenBudgetMillis
                                    val line =
                                            try {
                                                withTimeout(readBudgetMillis) {
                                                    withContext(Dispatchers.IO) { reader.readLine() }
                                                }
                                            } catch (timeout: TimeoutCancellationException) {
                                                call.cancel() // unblock the leaked blocking read
                                                throw if (emittedAny)
                                                        OpenAiStreamStalledException(
                                                                STREAM_IDLE_TIMEOUT_MILLIS / 1000
                                                        )
                                                else
                                                        OpenAiFirstTokenTimeoutException(
                                                                firstTokenBudgetMillis / 1000
                                                        )
                                            }
                                    if (line == null) break
                                    val payload =
                                            line.removePrefix("data:")
                                                    .trim()
                                                    .takeIf {
                                                        line.startsWith("data:") &&
                                                                it.isNotBlank() &&
                                                                it != "[DONE]"
                                                    }
                                                    ?: continue
                                    parseStreamChunk(payload, accumulator).forEach { event ->
                                        emittedAny = true
                                        emit(event)
                                    }
                                }
                            }
                        }
                        emit(LlmStreamEvent.Completed(accumulator.toResponse()))
                        return@flow
                    } catch (cancellation: CancellationException) {
                        throw cancellation
                    } catch (throwable: Throwable) {
                        // Only retry connection-phase failures: once any event has been
                        // emitted, retrying would duplicate streamed tokens.
                        if (!emittedAny && attempt < maxRetries && throwable.isRetryable()) {
                            delay(retryDelayMillis * (attempt + 1))
                            attempt += 1
                        } else {
                            throw throwable
                        }
                    } finally {
                        completionHandle?.dispose()
                    }
                }
            }

    private fun parseCompletionResponse(body: String): LlmResponse {
        val root = JsonParser.parseString(body).asJsonObject
        val choice =
                root.optArray("choices")?.firstOrNull()?.asJsonObject
                        ?: throw OpenAiEmptyResponseException()
        val message = choice.optObject("message") ?: throw OpenAiEmptyResponseException()
        val model = root.stringOrNull("model") ?: "unknown"
        return LlmResponse(
                id = root.stringOrNull("id"),
                model = model,
                message = message.toLlmMessage(),
                usage = root.optObject("usage")?.toTokenUsage(),
                finishReason = choice.stringOrNull("finish_reason").toFinishReason()
        )
    }

    private fun parseStreamChunk(
            payload: String,
            accumulator: StreamAccumulator
    ): List<LlmStreamEvent> {
        val root = JsonParser.parseString(payload).asJsonObject
        // Providers can open the stream with HTTP 200 and then emit an error object
        // (rate limit, quota) mid-stream. Surface it instead of silently ending the
        // stream with an empty completion.
        root.optObject("error")?.let { error ->
            val detail =
                    error.get("message")?.takeIf { !it.isJsonNull }?.asString?.takeIf {
                        it.isNotBlank()
                    }
            throw OpenAiClientException(
                    "OpenAI-compatible stream error: ${detail ?: "unknown provider error"}"
            )
        }
        root.stringOrNull("id")?.let { accumulator.id = it }
        root.stringOrNull("model")?.let { accumulator.model = it }
        root.optObject("usage")?.let { accumulator.usage = it.toTokenUsage() }

        val events = mutableListOf<LlmStreamEvent>()
        root.optArray("choices")?.forEach { choiceElement ->
            val choice = choiceElement.asJsonObject
            choice.stringOrNull("finish_reason")?.let {
                accumulator.finishReason = it.toFinishReason()
            }
            val delta = choice.optObject("delta") ?: return@forEach
            delta.stringOrNull("content")?.takeIf { it.isNotEmpty() }?.let {
                accumulator.text.append(it)
                events.add(LlmStreamEvent.TextDelta(it))
            }
            // Reasoning models (qwen3-thinking, DeepSeek-R1, ...) stream their chain of
            // thought in a separate reasoning_content field. Accumulate it so it is not
            // lost, but do not surface it as visible reply text.
            delta.stringOrNull("reasoning_content")?.takeIf { it.isNotEmpty() }?.let {
                accumulator.reasoning.append(it)
            }
            delta.optArray("tool_calls")?.forEachIndexed { index, toolCallElement ->
                val toolCall = toolCallElement.asJsonObject
                val toolIndex = toolCall.intOrNull("index") ?: index
                val builder =
                        accumulator.toolCalls.getOrPut(toolIndex) {
                            StreamToolCallBuilder(index = toolIndex)
                        }
                toolCall.stringOrNull("id")?.let { builder.id = it }
                toolCall.optObject("function")?.let { function ->
                    function.stringOrNull("name")?.let { builder.name = it }
                    function.stringOrNull("arguments")?.takeIf { it.isNotEmpty() }?.let {
                        builder.arguments.append(it)
                        builder.id?.let { id ->
                            events.add(LlmStreamEvent.ToolCallArgumentsDelta(id, it))
                        }
                    }
                }
                val startedCall = builder.toToolCallOrNull()
                if (startedCall != null && !builder.started) {
                    builder.started = true
                    events.add(LlmStreamEvent.ToolCallStarted(startedCall))
                }
            }
        }
        return events
    }

    private fun List<LlmMessage>.toOpenAiMessages(): JsonArray =
            JsonArray().also { array -> forEach { array.add(it.toOpenAiMessage()) } }

    private fun LlmMessage.toOpenAiMessage(): JsonObject =
            JsonObject().apply {
                addProperty("role", role.wireValue)
                name?.let { addProperty("name", it) }
                if (role == LlmMessageRole.TOOL) {
                    addProperty("content", textContent())
                    addProperty("tool_call_id", toolCallId)
                    return@apply
                }
                add("content", content.toOpenAiContent())
                if (toolCalls.isNotEmpty()) {
                    add("tool_calls", toolCalls.toOpenAiToolCalls())
                }
            }

    private fun List<LlmContentPart>.toOpenAiContent(): JsonElement {
        if (size == 1 && first() is LlmTextPart) {
            return gson.toJsonTree((first() as LlmTextPart).text)
        }
        if (isEmpty()) {
            return JsonNull.INSTANCE
        }
        return JsonArray().also { array ->
            forEach { part ->
                when (part) {
                    is LlmTextPart ->
                            array.add(
                                    JsonObject().apply {
                                        addProperty("type", "text")
                                        addProperty("text", part.text)
                                    }
                            )
                    is LlmImageUrlPart ->
                            array.add(
                                    JsonObject().apply {
                                        addProperty("type", "image_url")
                                        add(
                                                "image_url",
                                                JsonObject().apply {
                                                    addProperty("url", part.url)
                                                    part.detail?.let { addProperty("detail", it) }
                                                }
                                        )
                                    }
                            )
                }
            }
        }
    }

    private fun List<LlmToolCall>.toOpenAiToolCalls(): JsonArray =
            JsonArray().also { array ->
                forEach { toolCall ->
                    array.add(
                            JsonObject().apply {
                                addProperty("id", toolCall.id)
                                addProperty("type", "function")
                                add(
                                        "function",
                                        JsonObject().apply {
                                            addProperty("name", toolCall.name)
                                            addProperty("arguments", toolCall.argumentsJson)
                                        }
                                )
                            }
                    )
                }
            }

    private fun List<LlmToolDefinition>.toOpenAiTools(): JsonArray =
            JsonArray().also { array ->
                forEach { tool ->
                    array.add(
                            JsonObject().apply {
                                addProperty("type", "function")
                                add(
                                        "function",
                                        JsonObject().apply {
                                            addProperty("name", tool.name)
                                            addProperty("description", tool.description)
                                            add("parameters", gson.toJsonTree(tool.parameters))
                                        }
                                )
                            }
                    )
                }
            }

    private fun LlmToolChoice.toOpenAiToolChoice(): JsonElement =
            gson.toJsonTree(
                    when (this) {
                        LlmToolChoice.AUTO -> "auto"
                        LlmToolChoice.NONE -> "none"
                        LlmToolChoice.REQUIRED -> "required"
                    }
            )

    private fun JsonObject.toLlmMessage(): LlmMessage {
        val role =
                when (stringOrNull("role")) {
                    "system" -> LlmMessageRole.SYSTEM
                    "user" -> LlmMessageRole.USER
                    "tool" -> LlmMessageRole.TOOL
                    else -> LlmMessageRole.ASSISTANT
                }
        return LlmMessage(
                role = role,
                content = contentPartsFrom(get("content")),
                toolCallId = stringOrNull("tool_call_id"),
                toolCalls = optArray("tool_calls")?.toLlmToolCalls().orEmpty(),
                reasoningContent = stringOrNull("reasoning_content")
        )
    }

    private fun contentPartsFrom(value: JsonElement?): List<LlmContentPart> {
        if (value == null || value.isJsonNull) {
            return emptyList()
        }
        if (value.isJsonPrimitive) {
            return listOf(LlmTextPart(value.asString))
        }
        if (!value.isJsonArray) {
            return emptyList()
        }
        val parts = mutableListOf<LlmContentPart>()
        value.asJsonArray.forEach { element ->
            val obj = element.asJsonObject
            when (obj.stringOrNull("type")) {
                "text" -> obj.stringOrNull("text")?.let { parts.add(LlmTextPart(it)) }
                "image_url" -> {
                    val image = obj.optObject("image_url")
                    image?.stringOrNull("url")?.let {
                        parts.add(LlmImageUrlPart(url = it, detail = image.stringOrNull("detail")))
                    }
                }
            }
        }
        return parts
    }

    private fun JsonArray.toLlmToolCalls(): List<LlmToolCall> =
            mapIndexedNotNull { index, element ->
                val obj = element.asJsonObject
                val function = obj.optObject("function") ?: return@mapIndexedNotNull null
                LlmToolCall(
                        id = obj.stringOrNull("id") ?: "call_$index",
                        name = function.stringOrNull("name") ?: return@mapIndexedNotNull null,
                        argumentsJson = function.stringOrNull("arguments") ?: "{}"
                )
            }

    private fun JsonObject.toTokenUsage(): LlmTokenUsage =
            LlmTokenUsage(
                    inputTokens = intOrNull("prompt_tokens") ?: 0,
                    outputTokens = intOrNull("completion_tokens") ?: 0,
                    cachedInputTokens =
                            optObject("prompt_tokens_details")
                                    ?.intOrNull("cached_tokens")
                                    ?: 0
            )

    private fun String?.toFinishReason(): LlmFinishReason =
            when (this) {
                "tool_calls", "function_call" -> LlmFinishReason.TOOL_CALLS
                "length" -> LlmFinishReason.LENGTH
                "cancelled" -> LlmFinishReason.CANCELLED
                "error" -> LlmFinishReason.ERROR
                else -> LlmFinishReason.STOP
            }

    private fun clientFor(
            config: LlmGenerationConfig,
            forStreaming: Boolean = false
    ): OkHttpClient {
        // A null budget must NOT mean "wait forever" — fall back to a bounded default so no LLM call
        // can hang indefinitely on a stalled connection.
        val timeoutMillis = config.timeoutMillis ?: DEFAULT_CALL_TIMEOUT_MILLIS
        val connectTimeoutMillis = minOf(timeoutMillis, 30_000L)
        // readTimeout = max gap between bytes. For NON-streaming it must be the full budget: slow
        // reasoning models can hold the connection open well past OkHttp's default 10s before the only
        // byte arrives. For STREAMING it is instead a short IDLE guard (max gap between SSE chunks): a
        // stalled stream would otherwise block for the entire (often large) budget — the exact cause of
        // the planner hanging for minutes. A long but ACTIVE stream is unaffected since each chunk
        // resets the read timer.
        val readTimeoutMillis =
                if (forStreaming)
                // Loose socket backstop only — the PRECISE first-token vs inter-token timeouts (with
                // typed reasons) are enforced per-read in streamRequest. Keep this above both phases so
                // OkHttp's own SocketTimeout doesn't fire first and mask the typed reason.
                minOf(timeoutMillis, FIRST_TOKEN_TIMEOUT_MILLIS + STREAM_IDLE_TIMEOUT_MILLIS)
                else timeoutMillis
        val builder =
                httpClient
                        .newBuilder()
                        .readTimeout(readTimeoutMillis, TimeUnit.MILLISECONDS)
                        .writeTimeout(timeoutMillis, TimeUnit.MILLISECONDS)
                        .connectTimeout(connectTimeoutMillis, TimeUnit.MILLISECONDS)
        // Hard total-call ceiling only for non-streaming; for streaming the idle read timeout is the
        // guard (a hard callTimeout would truncate a legitimately long generation mid-stream).
        if (!forStreaming) {
            builder.callTimeout(timeoutMillis, TimeUnit.MILLISECONDS)
        }
        return builder.build()
    }

    private data class StreamAccumulator(
            var model: String,
            var id: String? = null,
            val text: StringBuilder = StringBuilder(),
            val reasoning: StringBuilder = StringBuilder(),
            val toolCalls: MutableMap<Int, StreamToolCallBuilder> = linkedMapOf(),
            var usage: LlmTokenUsage? = null,
            var finishReason: LlmFinishReason = LlmFinishReason.STOP
    ) {
        fun toResponse(): LlmResponse {
            val calls = toolCalls.values.mapNotNull { it.toToolCallOrNull() }
            val reasoningContent = reasoning.toString().takeIf { it.isNotBlank() }
            val message =
                    if (calls.isEmpty()) {
                        LlmMessage.assistant(text.toString())
                                .copy(reasoningContent = reasoningContent)
                    } else {
                        LlmMessage.assistantToolCalls(
                                toolCalls = calls,
                                text = text.toString().takeIf { it.isNotBlank() }
                        ).copy(reasoningContent = reasoningContent)
                    }
            return LlmResponse(
                    id = id,
                    model = model,
                    message = message,
                    usage = usage,
                    finishReason = if (calls.isEmpty()) finishReason else LlmFinishReason.TOOL_CALLS
            )
        }
    }

    private data class StreamToolCallBuilder(
            val index: Int = 0,
            var id: String? = null,
            var name: String? = null,
            val arguments: StringBuilder = StringBuilder(),
            var started: Boolean = false
    ) {
        fun toToolCallOrNull(): LlmToolCall? {
            val callName = name?.takeIf { it.isNotBlank() } ?: return null
            // Some OpenAI-compatible providers (DashScope/qwen) OMIT the tool_call `id` in STREAMING
            // deltas (it's optional in the spec). Requiring it dropped the whole tool call → the
            // planner's `ask_ai_agent`/`replier` call vanished, finish_reason=TOOL_CALLS but an empty
            // list, and forceReplier masked it as a hallucinated direct reply. Synthesize a stable id
            // from the call index instead so the call survives (it stays consistent across the
            // assistant message + its tool_result within the turn).
            val callId = id?.takeIf { it.isNotBlank() } ?: "call_${index}_$callName"
            return LlmToolCall(
                    id = callId,
                    name = callName,
                    argumentsJson = arguments.toString().ifBlank { "{}" }
            )
        }
    }

    companion object {
        // Fallback overall ceiling for one LLM call when the caller supplies no budget.
        private const val DEFAULT_CALL_TIMEOUT_MILLIS = 120_000L
        // PHASE 2 — time to the FIRST streamed token (request sent → model starts replying). A
        // responsive (domestic) API starts emitting within a few seconds, so 30s is ample slack for
        // brief slowness while still failing a STUCK request FAST — fast enough that the retries below
        // actually recover a transient hiccup instead of waiting out a long hang before giving up.
        private const val FIRST_TOKEN_TIMEOUT_MILLIS = 30_000L
        // PHASE 3 — max gap BETWEEN streamed tokens once the model has started replying. Short: an
        // active stream resets it every token, so this only fires when a started stream STALLS. Kept
        // distinct from FIRST_TOKEN so long thinking is never misread as a stall (the old bug).
        private const val STREAM_IDLE_TIMEOUT_MILLIS = 30_000L

        private fun normalizeChatCompletionsUrl(baseUrl: String): String {
            val normalized = baseUrl.trim().trimEnd('/')
            require(normalized.isNotBlank()) { "OpenAI-compatible baseUrl must not be blank" }
            return if (normalized.endsWith("/chat/completions")) {
                normalized
            } else {
                "$normalized/chat/completions"
            }
        }
    }
}

open class OpenAiClientException(
        message: String,
        cause: Throwable? = null
) : Exception(message, cause)

class OpenAiHttpException(
        val statusCode: Int,
        val responseBody: String
) : OpenAiClientException(buildHttpExceptionMessage(statusCode, responseBody)) {
    companion object {
        private fun buildHttpExceptionMessage(statusCode: Int, body: String): String {
            // Surface the provider's own error message (e.g. "model not found",
            // "insufficient quota") instead of an opaque "HTTP 4xx".
            val detail =
                    runCatching {
                                JsonParser.parseString(body)
                                        .asJsonObject
                                        .optObject("error")
                                        ?.get("message")
                                        ?.takeIf { !it.isJsonNull }
                                        ?.asString
                            }
                            .getOrNull()
                            ?.takeIf { it.isNotBlank() }
            return if (detail != null) {
                "OpenAI-compatible endpoint returned HTTP $statusCode: $detail"
            } else {
                "OpenAI-compatible endpoint returned HTTP $statusCode"
            }
        }
    }
}

class OpenAiEmptyResponseException : OpenAiClientException("OpenAI-compatible endpoint returned an empty response")

class OpenAiToolLoopException(message: String) : OpenAiClientException(message)

/** Phase 2 failure: the model never produced a first token (request/connection stuck, NOT thinking). */
class OpenAiFirstTokenTimeoutException(seconds: Long) :
        IOException(
                "LLM produced no first token within ${seconds}s — request/connection stalled (not productive thinking)"
        )

/** Phase 3 failure: a stream that had started replying then stalled (no token within the idle gap). */
class OpenAiStreamStalledException(seconds: Long) :
        IOException("LLM stream stalled — no token for ${seconds}s after it had started replying")

// Null-SAFE object/array member access. Gson's getAsJsonObject/getAsJsonArray cast the member and
// THROW ClassCastException when it is JsonNull (e.g. dashscope SSE chunks carry "usage":null), which
// used to crash the streaming replier on the very first chunk. These return null instead.
private fun JsonObject.optObject(name: String): JsonObject? =
        get(name)?.takeIf { it.isJsonObject }?.asJsonObject

private fun JsonObject.optArray(name: String): JsonArray? =
        get(name)?.takeIf { it.isJsonArray }?.asJsonArray

private fun JsonObject.stringOrNull(name: String): String? =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asString

private fun JsonObject.intOrNull(name: String): Int? =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asInt
