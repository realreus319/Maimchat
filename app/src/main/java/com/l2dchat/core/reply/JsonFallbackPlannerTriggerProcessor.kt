package com.l2dchat.core.reply

import com.google.gson.Gson
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolResult
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.tools.ToolRegistry

class JsonFallbackPlannerTriggerProcessor(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val toolRegistry: ToolRegistry,
        private val promptBuilder: PlannerPromptBuilder = PlannerPromptBuilder(),
        private val toolMode: ToolExecutionMode = ToolExecutionMode.NORMAL,
        private val gson: Gson = Gson()
) : PlannerTriggerProcessor {
    init {
        require(toolRegistry.definitionsFor(toolMode).isNotEmpty()) {
            "JsonFallbackPlannerTriggerProcessor requires at least one ${toolMode.name.lowercase()} tool"
        }
    }

    override suspend fun process(context: PlannerTurnContext) {
        val toolDefinitions = toolRegistry.definitionsFor(toolMode)
        val history = promptBuilder.buildMessages(context).withJsonFallbackInstructions(toolDefinitions)
        val toolContext =
                ToolExecutionContext(
                        loopId = context.loopId,
                        routingKey = context.routingKey,
                        trigger = context.trigger,
                        foregroundEpoch = context.foregroundEpoch,
                        mode = toolMode
                )

        repeat(config.maxToolRounds + 1) { round ->
            val response = llmClient.chatCompletion(messages = history, config = config)
            val command = parseCommand(response.text)

            when (command) {
                is JsonFallbackCommand.Final -> {
                    val result = context.sendReply(command.text)
                    if (result.status == ReplySendStatus.BLANK_REJECTED) {
                        throw IllegalStateException("JSON fallback final response was blank")
                    }
                    return
                }
                is JsonFallbackCommand.ToolCall -> {
                    if (round >= config.maxToolRounds) {
                        throw IllegalStateException(
                                "LLM returned JSON tool calls after ${config.maxToolRounds} tool rounds"
                        )
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
                        if (sendResult.sent) {
                            return
                        }
                    }
                    history.add(response.message)
                    history.add(LlmMessage.user(execution.toLlmToolResult().toFallbackPrompt()))
                }
            }
        }

        throw IllegalStateException("JSON fallback planner loop ended without a final response")
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

    private fun String.extractObjectText(): String? {
        if (startsWith("{") && endsWith("}")) {
            return this
        }
        val start = indexOf('{')
        val end = lastIndexOf('}')
        return if (start >= 0 && end > start) substring(start, end + 1) else null
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
