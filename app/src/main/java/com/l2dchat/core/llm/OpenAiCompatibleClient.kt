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
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
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
        private val maxRetries: Int = 1,
        private val retryDelayMillis: Long = 250L,
        private val extraHeaders: Map<String, String> = emptyMap()
) : LlmClient {
    private val chatCompletionsUrl = normalizeChatCompletionsUrl(baseUrl)

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
            val response =
                    executeWithRetry(config) {
                        val body =
                                buildRequestBody(
                                        messages = history,
                                        tools = tools,
                                        config = config,
                                        stream = false
                                )
                        val request = buildRequest(body, config)
                        parseCompletionResponse(executeRequest(request, config))
                    }
            lastResponse = response
            if (response.toolCalls.isEmpty()) {
                return response
            }
            if (round >= config.maxToolRounds) {
                throw OpenAiToolLoopException(
                        "LLM returned tool calls after ${config.maxToolRounds} tool rounds"
                )
            }

            history.add(response.message)
            response.toolCalls.forEach { toolCall ->
                history.add(LlmMessage.toolResult(toolExecutor.execute(toolCall)))
            }
        }

        throw OpenAiToolLoopException("LLM tool loop ended without a final response: $lastResponse")
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
        config.timeoutMillis?.let { builder.tag(RequestTimeout::class.java, RequestTimeout(it)) }
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
                val call = clientFor(config).newCall(request)
                val completionHandle =
                        currentCoroutineContext()[Job]?.invokeOnCompletion { cause ->
                            if (cause is CancellationException) {
                                call.cancel()
                            }
                        }
                val accumulator = StreamAccumulator(config.model)
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
                                val line = withContext(Dispatchers.IO) { reader.readLine() } ?: break
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
                                    emit(event)
                                }
                            }
                        }
                    }
                    emit(LlmStreamEvent.Completed(accumulator.toResponse()))
                } finally {
                    completionHandle?.dispose()
                }
            }

    private fun parseCompletionResponse(body: String): LlmResponse {
        val root = JsonParser.parseString(body).asJsonObject
        val choice =
                root.getAsJsonArray("choices")?.firstOrNull()?.asJsonObject
                        ?: throw OpenAiEmptyResponseException()
        val message = choice.getAsJsonObject("message") ?: throw OpenAiEmptyResponseException()
        val model = root.stringOrNull("model") ?: "unknown"
        return LlmResponse(
                id = root.stringOrNull("id"),
                model = model,
                message = message.toLlmMessage(),
                usage = root.getAsJsonObject("usage")?.toTokenUsage(),
                finishReason = choice.stringOrNull("finish_reason").toFinishReason()
        )
    }

    private fun parseStreamChunk(
            payload: String,
            accumulator: StreamAccumulator
    ): List<LlmStreamEvent> {
        val root = JsonParser.parseString(payload).asJsonObject
        root.stringOrNull("id")?.let { accumulator.id = it }
        root.stringOrNull("model")?.let { accumulator.model = it }
        root.getAsJsonObject("usage")?.let { accumulator.usage = it.toTokenUsage() }

        val events = mutableListOf<LlmStreamEvent>()
        root.getAsJsonArray("choices")?.forEach { choiceElement ->
            val choice = choiceElement.asJsonObject
            choice.stringOrNull("finish_reason")?.let {
                accumulator.finishReason = it.toFinishReason()
            }
            val delta = choice.getAsJsonObject("delta") ?: return@forEach
            delta.stringOrNull("content")?.takeIf { it.isNotEmpty() }?.let {
                accumulator.text.append(it)
                events.add(LlmStreamEvent.TextDelta(it))
            }
            delta.getAsJsonArray("tool_calls")?.forEachIndexed { index, toolCallElement ->
                val toolCall = toolCallElement.asJsonObject
                val toolIndex = toolCall.intOrNull("index") ?: index
                val builder = accumulator.toolCalls.getOrPut(toolIndex) { StreamToolCallBuilder() }
                toolCall.stringOrNull("id")?.let { builder.id = it }
                toolCall.getAsJsonObject("function")?.let { function ->
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
                toolCalls = getAsJsonArray("tool_calls")?.toLlmToolCalls().orEmpty()
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
                    val image = obj.getAsJsonObject("image_url")
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
                val function = obj.getAsJsonObject("function") ?: return@mapIndexedNotNull null
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
                            getAsJsonObject("prompt_tokens_details")
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

    private fun clientFor(config: LlmGenerationConfig): OkHttpClient {
        val timeoutMillis = config.timeoutMillis ?: return httpClient
        return httpClient.newBuilder().callTimeout(timeoutMillis, TimeUnit.MILLISECONDS).build()
    }

    private data class RequestTimeout(val timeoutMillis: Long)

    private data class StreamAccumulator(
            var model: String,
            var id: String? = null,
            val text: StringBuilder = StringBuilder(),
            val toolCalls: MutableMap<Int, StreamToolCallBuilder> = linkedMapOf(),
            var usage: LlmTokenUsage? = null,
            var finishReason: LlmFinishReason = LlmFinishReason.STOP
    ) {
        fun toResponse(): LlmResponse {
            val calls = toolCalls.values.mapNotNull { it.toToolCallOrNull() }
            val message =
                    if (calls.isEmpty()) {
                        LlmMessage.assistant(text.toString())
                    } else {
                        LlmMessage.assistantToolCalls(
                                toolCalls = calls,
                                text = text.toString().takeIf { it.isNotBlank() }
                        )
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
            var id: String? = null,
            var name: String? = null,
            val arguments: StringBuilder = StringBuilder(),
            var started: Boolean = false
    ) {
        fun toToolCallOrNull(): LlmToolCall? {
            val callId = id?.takeIf { it.isNotBlank() } ?: return null
            val callName = name?.takeIf { it.isNotBlank() } ?: return null
            return LlmToolCall(
                    id = callId,
                    name = callName,
                    argumentsJson = arguments.toString().ifBlank { "{}" }
            )
        }
    }

    companion object {
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
) : OpenAiClientException("OpenAI-compatible endpoint returned HTTP $statusCode")

class OpenAiEmptyResponseException : OpenAiClientException("OpenAI-compatible endpoint returned an empty response")

class OpenAiToolLoopException(message: String) : OpenAiClientException(message)

private fun JsonObject.stringOrNull(name: String): String? =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asString

private fun JsonObject.intOrNull(name: String): Int? =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asInt
