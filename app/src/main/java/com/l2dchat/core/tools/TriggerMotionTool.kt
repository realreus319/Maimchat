package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.MotionTriggerRequest
import com.l2dchat.core.environment.MotionTriggerResult
import com.l2dchat.core.environment.NoopMotionController
import com.l2dchat.core.llm.LlmToolDefinition

class TriggerMotionTool(
        private val motionController: MotionController = NoopMotionController,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Trigger a Live2D motion by group and index, or by motion file path, as part of the current reply plan.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "properties" to
                                            mapOf(
                                                    "group" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Live2D motion group, for example Idle or TapBody."
                                                            ),
                                                    "index" to
                                                            mapOf(
                                                                    "type" to "integer",
                                                                    "minimum" to 0,
                                                                    "description" to
                                                                            "Motion index inside the group. Required when group is provided."
                                                            ),
                                                    "file_path" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional motion file path from get_world_state. Used when group is not provided."
                                                            ),
                                                    "loop" to
                                                            mapOf(
                                                                    "type" to "boolean",
                                                                    "description" to
                                                                            "Whether the motion should loop. Defaults to false."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val request = arguments.toMotionTriggerRequest()
        val result = motionController.triggerMotion(context, request)
        val response =
                MotionToolResponse(
                        accepted = result.accepted,
                        group = request.group,
                        index = request.index,
                        filePath = request.filePath,
                        loop = request.loop,
                        message = result.message,
                        metadata = result.metadata.takeIf { it.isNotEmpty() }
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                isError = !result.accepted,
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "accepted" to result.accepted,
                                "group" to request.group,
                                "index" to request.index,
                                "file_path" to request.filePath,
                                "loop" to request.loop
                        ) + result.metadata
        )
    }

    private fun JsonObject.toMotionTriggerRequest(): MotionTriggerRequest {
        val group = stringOrNull("group")?.trimOrNull()
        val filePath = stringOrNull("file_path")?.trimOrNull()
        val index = intOrNull("index")
        require(group != null || filePath != null) {
            "trigger_motion requires either group or file_path"
        }
        require(group == null || index != null) {
            "trigger_motion.index is required when group is provided"
        }
        return MotionTriggerRequest(
                group = group,
                index = index,
                filePath = filePath,
                loop = booleanOrDefault("loop", false)
        )
    }

    private data class MotionToolResponse(
            val accepted: Boolean,
            val group: String?,
            val index: Int?,
            val filePath: String?,
            val loop: Boolean,
            val message: String,
            val metadata: Map<String, Any?>?
    )

    companion object {
        const val NAME: String = "trigger_motion"
    }
}

private fun JsonObject.stringOrNull(name: String): String? {
    val element = get(name) ?: return null
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isString) {
        "$name must be a string"
    }
    return element.asString
}

private fun JsonObject.intOrNull(name: String): Int? {
    val element = get(name) ?: return null
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isNumber) {
        "$name must be an integer"
    }
    val value = element.asInt
    require(value >= 0) { "$name must be non-negative" }
    return value
}

private fun JsonObject.booleanOrDefault(name: String, defaultValue: Boolean): Boolean {
    val element = get(name) ?: return defaultValue
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isBoolean) {
        "$name must be a boolean"
    }
    return element.asBoolean
}

private fun String.trimOrNull(): String? = trim().takeIf { it.isNotBlank() }
