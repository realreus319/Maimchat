package com.l2dchat.core.reply

import com.google.gson.Gson
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolResult
import com.l2dchat.core.tools.ReplierTool
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.tools.ToolRegistry

class JsonFallbackPlannerTriggerProcessor(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val toolRegistry: ToolRegistry,
        private val promptBuilder: PlannerPromptBuilder = PlannerPromptBuilder(),
        private val systemPromptProvider: PlannerSystemPromptProvider =
                EmptyPlannerSystemPromptProvider,
        private val toolMode: ToolExecutionMode = ToolExecutionMode.NORMAL,
        private val gson: Gson = Gson(),
        private val mediaReader: LtmMediaReader = LtmMediaReader.DEFAULT,
) : PlannerTriggerProcessor {
    init {
        require(toolRegistry.definitionsFor(toolMode).isNotEmpty()) {
            "JsonFallbackPlannerTriggerProcessor requires at least one ${toolMode.name.lowercase()} tool"
        }
    }

    override suspend fun process(context: PlannerTurnContext) {
        val toolDefinitions = toolRegistry.definitionsFor(toolMode)
        val history =
                promptBuilder
                        .buildMessages(
                                context = context,
                                systemPromptOverride = systemPromptProvider.systemPromptFor(context)
                        )
                        .withJsonFallbackInstructions(toolDefinitions)
        val toolContext =
                ToolExecutionContext(
                        loopId = context.loopId,
                        routingKey = context.routingKey,
                        trigger = context.trigger,
                        foregroundEpoch = context.foregroundEpoch,
                        roundId = context.roundId,
                        mode = toolMode
                )

        repeat(config.maxToolRounds + 1) { round ->
            val response = llmClient.chatCompletion(messages = history, config = config)
            val command = parseCommand(response.text)

            when (command) {
                is JsonFallbackCommand.Final -> {
                    // ALL replies MUST go through the replier — never surface the planner's own
                    // "final" text directly. Route it through the replier as its `thinking`.
                    finalizeViaReplier(toolContext, context, command.text.trim().ifBlank { null })
                    return
                }
                is JsonFallbackCommand.ToolCall -> {
                    if (round >= config.maxToolRounds) {
                        // Tool budget exhausted; force the replier to compose the reply.
                        finalizeViaReplier(toolContext, context, null)
                        return
                    }
                    val execution =
                            toolRegistry.execute(
                                    toolContext,
                                    LlmToolCall(
                                            id = "json_fallback_$round",
                                            name = command.name,
                                            argumentsJson = command.argumentsJson
                                    )
                            )
                    if (!execution.result.isError && execution.result.replyText != null) {
                        val sendResult = context.sendReply(execution.result.replyText)
                        // SENT: done. STALE/DUPLICATE: the turn was superseded or already
                        // answered, so stop rather than dropping the reply and re-prompting.
                        if (sendResult.status != ReplySendStatus.BLANK_REJECTED) {
                            return
                        }
                    }
                    history.add(response.message)
                    val toolResult = execution.toLlmToolResult()
                    val fallbackText = toolResult.toFallbackPrompt()
                    val mediaParts = toolResult.ltmReadMediaPhotoParts(mediaReader)
                    if (mediaParts != null) {
                        // T5: photo result — emit the fallback text plus the inlined image as a
                        // multimodal user message (the JSON fallback path has no native tool
                        // message, so the image rides on the same user turn).
                        history.add(
                                LlmMessage(
                                        role = LlmMessageRole.USER,
                                        content = listOf(LlmTextPart(fallbackText)) + mediaParts,
                                )
                        )
                    } else {
                        history.add(LlmMessage.user(fallbackText))
                    }
                }
            }
        }

        finalizeViaReplier(toolContext, context, null)
    }

    /** Force the reply through the replier and send it; never a planner-authored direct reply. */
    private suspend fun finalizeViaReplier(
            toolContext: ToolExecutionContext,
            context: PlannerTurnContext,
            plannerThinking: String?
    ) {
        val replyText = forceReplier(toolContext, context, plannerThinking)
        val result = context.sendReply(replyText)
        if (result.status == ReplySendStatus.BLANK_REJECTED) {
            throw IllegalStateException(
                    "replier produced no reply even after a forced replier call (compat mode)"
            )
        }
    }

    /**
     * Execute the [ReplierTool] (with a couple of retries) and return its composed reply text — or ""
     * if it persistently fails. [plannerThinking] (the planner's own text) is passed as the replier's
     * `thinking` so any tool results the planner narrated reach the replier.
     */
    private suspend fun forceReplier(
            toolContext: ToolExecutionContext,
            context: PlannerTurnContext,
            plannerThinking: String?
    ): String {
        val thinking =
                plannerThinking
                        ?: "请根据当前对话上下文和已完成的工具结果，用角色口吻直接回复用户。"
        val argumentsJson =
                JsonObject().apply { addProperty("thinking", thinking) }.toString()
        repeat(REPLIER_FORCE_ATTEMPTS) { attempt ->
            val call =
                    LlmToolCall(
                            id = "forced-replier-${context.roundId}-$attempt",
                            name = ReplierTool.NAME,
                            argumentsJson = argumentsJson
                    )
            val execution = runCatching { toolRegistry.execute(toolContext, call) }.getOrNull()
            val text = execution?.result?.replyText?.trim()?.ifBlank { null }
            if (execution != null && !execution.result.isError && text != null) {
                return text
            }
        }
        return ""
    }

    companion object {
        private const val REPLIER_FORCE_ATTEMPTS = 2
    }

    private fun List<LlmMessage>.withJsonFallbackInstructions(
            toolDefinitions: List<LlmToolDefinition>
    ): MutableList<LlmMessage> {
        val history = toMutableList()
        val insertionIndex = history.indexOfFirst { it.role == LlmMessageRole.USER }.takeIf { it >= 0 }
                ?: history.size
        history.add(insertionIndex, LlmMessage.system(jsonFallbackInstructions(toolDefinitions)))
        return history
    }

    private fun jsonFallbackInstructions(toolDefinitions: List<LlmToolDefinition>): String =
            buildString {
                append("Native tool calling is unavailable. Respond with JSON only, no markdown.\n")
                append("Use one of these shapes:\n")
                append("""{"type":"tool_call","tool":"tool_name","arguments":{}}""")
                append('\n')
                append("""{"type":"final","text":"final reply text"}""")
                append("\nAvailable tools:\n")
                toolDefinitions.forEach { tool ->
                    append("- ")
                    append(tool.name)
                    append(": ")
                    append(tool.description)
                    append("\n  parameters: ")
                    append(gson.toJson(tool.parameters))
                    append('\n')
                }
            }

    private fun LlmToolResult.toFallbackPrompt(): String =
            buildString {
                append("[tool_result]\n")
                append("tool: ")
                append(name)
                append('\n')
                append("is_error: ")
                append(isError)
                append('\n')
                append("content: ")
                append(content)
                append("\n[/tool_result]\n")
                append("Continue with JSON only.")
            }

    private fun parseCommand(text: String): JsonFallbackCommand {
        val json = parseJsonObject(text)
                ?: return JsonFallbackCommand.Final(text.trim())
        val type = json.stringOrNull("type")?.lowercase()
        return when (type) {
            "tool", "tool_call" ->
                    JsonFallbackCommand.ToolCall(
                            name =
                                    json.stringOrNull("tool")
                                            ?: json.stringOrNull("name")
                                            ?: throw IllegalArgumentException(
                                                    "JSON fallback tool call is missing tool"
                                            ),
                            argumentsJson = argumentsJson(json.get("arguments"))
                    )
            "final", "final_response" ->
                    JsonFallbackCommand.Final(
                            json.stringOrNull("text")
                                    ?: json.stringOrNull("content")
                                    ?: ""
                    )
            else ->
                    if (json.has("tool") || json.has("name")) {
                        JsonFallbackCommand.ToolCall(
                                name =
                                        json.stringOrNull("tool")
                                                ?: json.stringOrNull("name")
                                                ?: throw IllegalArgumentException(
                                                        "JSON fallback tool call is missing tool"
                                                ),
                                argumentsJson = argumentsJson(json.get("arguments"))
                        )
                    } else {
                        JsonFallbackCommand.Final(
                                json.stringOrNull("text")
                                        ?: json.stringOrNull("content")
                                        ?: text.trim()
                        )
                    }
        }
    }

    private fun parseJsonObject(text: String): JsonObject? {
        val candidate = text.trim().removeJsonFence().extractObjectText() ?: return null
        return try {
            JsonParser.parseString(candidate).asJsonObject
        } catch (_: JsonSyntaxException) {
            null
        } catch (_: IllegalStateException) {
            null
        }
    }

    private fun String.removeJsonFence(): String {
        if (!startsWith("```")) {
            return this
        }
        return lines()
                .drop(1)
                .dropLastWhile { it.trim() == "```" }
                .joinToString("\n")
                .trim()
    }

    /**
     * Extract the first complete top-level JSON object by tracking brace depth while
     * respecting string literals/escapes. This survives reasoning-model output that wraps
     * the action JSON in prose or trailing explanation (where a naive first-`{`/last-`}`
     * span would capture invalid JSON and leak the raw text back to the user).
     */
    private fun String.extractObjectText(): String? {
        val start = indexOf('{')
        if (start < 0) return null
        var depth = 0
        var inString = false
        var escaped = false
        var i = start
        while (i < length) {
            val c = this[i]
            if (inString) {
                when {
                    escaped -> escaped = false
                    c == '\\' -> escaped = true
                    c == '"' -> inString = false
                }
            } else {
                when (c) {
                    '"' -> inString = true
                    '{' -> depth++
                    '}' -> {
                        depth--
                        if (depth == 0) return substring(start, i + 1)
                    }
                }
            }
            i++
        }
        return null
    }

    private fun argumentsJson(value: JsonElement?): String {
        if (value == null || value.isJsonNull) {
            return "{}"
        }
        return when {
            value.isJsonObject -> gson.toJson(value)
            value.isJsonPrimitive && value.asJsonPrimitive.isString ->
                    value.asString.takeIf { it.isNotBlank() } ?: "{}"
            else -> throw IllegalArgumentException("JSON fallback arguments must be an object")
        }
    }

    private fun JsonObject.stringOrNull(name: String): String? =
            get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asString
}

private sealed interface JsonFallbackCommand {
    data class ToolCall(
            val name: String,
            val argumentsJson: String
    ) : JsonFallbackCommand

    data class Final(val text: String) : JsonFallbackCommand
}

/**
 * T5 (JSON-fallback path): returns the `image_url` content parts to inject for an
 * `ltm_read_media` photo result, or `null` when the result is not a photo / has an error / the
 * file cannot be read. Mirrors [ToolCallingPlannerTriggerProcessor]'s injection but returns the
 * parts directly (the fallback path has no native tool message to attach them to).
 */
private fun LlmToolResult.ltmReadMediaPhotoParts(
        mediaReader: LtmMediaReader,
): List<LlmContentPart>? {
    if (isError || name != LTM_READ_MEDIA_TOOL) return null
    val parsed = runCatching { JsonParser.parseString(content).asJsonObject }.getOrNull() ?: return null
    if (parsed.get("error")?.takeIf { !it.isJsonNull } != null) return null
    val modality = parsed.get("modality")?.takeIf { !it.isJsonNull }?.asString
    if (modality != "photo") return null
    val mediaPath = parsed.get("media_path")?.takeIf { !it.isJsonNull }?.asString
    if (mediaPath.isNullOrBlank()) return null
    val dataUrl = runCatching { mediaReader.readDataUrl(mediaPath) }.getOrNull() ?: return null
    return listOf(
            LlmTextPart("[media injected: $mediaPath]"),
            LlmImageUrlPart(url = dataUrl),
    )
}

private const val LTM_READ_MEDIA_TOOL: String = "ltm_read_media"
