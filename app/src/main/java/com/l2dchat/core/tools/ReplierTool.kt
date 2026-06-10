package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.llm.LlmToolDefinition

class ReplierTool(
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Prepare the exact chat reply text. In planner-managed mode this tool returns the reply text and does not send it directly.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "required" to listOf("content"),
                                    "properties" to
                                            mapOf(
                                                    "content" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "The exact message content to send to the user."
                                                            ),
                                                    "reply_guidance" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional notes for how this reply was chosen."
                                                            ),
                                                    "style_override" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional style instruction used for this reply."
                                                            ),
                                                    "emotion_hint" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional emotion hint for downstream Live2D behavior."
                                                            ),
                                                    "is_progress_update" to
                                                            mapOf(
                                                                    "type" to "boolean",
                                                                    "description" to
                                                                            "Whether the content is only a temporary progress update."
                                                            ),
                                                    "include_action" to
                                                            mapOf(
                                                                    "type" to "boolean",
                                                                    "description" to
                                                                            "Whether a matching avatar action should be considered."
                                                            ),
                                                    "live_image" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional live image reference or identifier."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val content = arguments.stringOrNull("content")?.trim().orEmpty()
        require(content.isNotBlank()) { "replier.content must not be blank" }

        val resultJson =
                JsonObject().apply {
                    addProperty("replyText", content)
                    addProperty("sent", false)
                    arguments.stringOrNull("reply_guidance")?.let {
                        addProperty("replyGuidance", it)
                    }
                    arguments.stringOrNull("style_override")?.let {
                        addProperty("styleOverride", it)
                    }
                    arguments.stringOrNull("emotion_hint")?.let {
                        addProperty("emotionHint", it)
                    }
                    arguments.booleanOrNull("is_progress_update")?.let {
                        addProperty("isProgressUpdate", it)
                    }
                    arguments.booleanOrNull("include_action")?.let {
                        addProperty("includeAction", it)
                    }
                    arguments.stringOrNull("live_image")?.let { addProperty("liveImage", it) }
                }
        return ToolExecutionResult(
                llmContent = gson.toJson(resultJson),
                replyText = content,
                sent = false,
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "planner_managed" to true,
                                "trigger_message_id" to context.trigger.messageId
                        )
        )
    }

    companion object {
        const val NAME: String = "replier"
    }
}

private fun JsonObject.stringOrNull(name: String): String? =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asString

private fun JsonObject.booleanOrNull(name: String): Boolean? =
        get(name)
                ?.takeIf { !it.isJsonNull && it.isJsonPrimitive && it.asJsonPrimitive.isBoolean }
                ?.asBoolean
