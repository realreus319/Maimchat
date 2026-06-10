package com.l2dchat.core.tools

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolResult

class ToolRegistry(
        tools: List<Tool>
) {
    private val toolsByName: Map<String, Tool> =
            tools.associateBy { it.definition.name }.also { byName ->
                require(byName.size == tools.size) { "Duplicate tool names are not allowed" }
            }

    val definitions: List<LlmToolDefinition> = definitionsFor(ToolExecutionMode.NORMAL)

    fun definitionsFor(mode: ToolExecutionMode): List<LlmToolDefinition> =
            toolsByName.values
                    .filter { mode in it.allowedModes }
                    .map { it.definition }

    suspend fun execute(
            context: ToolExecutionContext,
            toolCall: LlmToolCall
    ): ToolCallExecutionResult {
        val tool =
                toolsByName[toolCall.name]
                        ?: return ToolCallExecutionResult(
                                toolCall = toolCall,
                                result =
                                        ToolExecutionResult(
                                                llmContent = "Unknown tool: ${toolCall.name}",
                                                isError = true
                                        )
                        )
        if (context.mode !in tool.allowedModes) {
            return ToolCallExecutionResult(
                    toolCall = toolCall,
                    result =
                            ToolExecutionResult(
                                    llmContent =
                                            "Tool ${toolCall.name} is not allowed in ${context.mode.name.lowercase()} mode",
                                    isError = true
                            )
            )
        }
        val arguments =
                parseArguments(toolCall.argumentsJson)
                        ?: return ToolCallExecutionResult(
                                toolCall = toolCall,
                                result =
                                        ToolExecutionResult(
                                                llmContent = "Invalid JSON arguments for ${toolCall.name}",
                                                isError = true
                                        )
                        )
        val result =
                try {
                    tool.execute(context, arguments)
                } catch (error: IllegalArgumentException) {
                    ToolExecutionResult(
                            llmContent = error.message ?: "Invalid arguments for ${toolCall.name}",
                            isError = true
                    )
                }
        return ToolCallExecutionResult(toolCall = toolCall, result = result)
    }

    private fun parseArguments(argumentsJson: String): JsonObject? =
            try {
                val element = JsonParser.parseString(argumentsJson)
                if (element != null && element.isJsonObject) {
                    element.asJsonObject
                } else {
                    null
                }
            } catch (_: JsonSyntaxException) {
                null
            } catch (_: IllegalStateException) {
                null
            }
}

data class ToolCallExecutionResult(
        val toolCall: LlmToolCall,
        val result: ToolExecutionResult
) {
    fun toLlmToolResult(): LlmToolResult =
            LlmToolResult(
                    toolCallId = toolCall.id,
                    name = toolCall.name,
                    content = result.llmContent,
                    isError = result.isError
            )
}
