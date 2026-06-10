package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentChatBubble
import com.l2dchat.core.environment.EnvironmentInteraction
import com.l2dchat.core.environment.EnvironmentModelState
import com.l2dchat.core.environment.EnvironmentState
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentSurfaceState
import com.l2dchat.core.environment.EnvironmentVisualSnapshot
import com.l2dchat.core.llm.LlmToolDefinition

class LookAtTool(
        private val stateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Inspect current visual environment metadata. This first Android version returns metadata only and does not attach screenshot pixels.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "properties" to
                                            mapOf(
                                                    "focus" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional area to inspect, such as model, screen, wallpaper, background, interaction, or chat."
                                                            ),
                                                    "include_model" to
                                                            lookAtBooleanParameter(
                                                                    "Whether to include selected Live2D model metadata. Defaults to true."
                                                            ),
                                                    "include_surface" to
                                                            lookAtBooleanParameter(
                                                                    "Whether to include app, wallpaper, and background surface metadata. Defaults to true."
                                                            ),
                                                    "include_last_interaction" to
                                                            lookAtBooleanParameter(
                                                                    "Whether to include latest touch or drag metadata. Defaults to true."
                                                            ),
                                                    "include_snapshot_metadata" to
                                                            lookAtBooleanParameter(
                                                                    "Whether to include available visual snapshot metadata. Defaults to true."
                                                            ),
                                                    "include_recent_bubbles" to
                                                            lookAtBooleanParameter(
                                                                    "Whether to include recent visible chat bubble text. Defaults to false."
                                                            ),
                                                    "max_recent_bubbles" to
                                                            mapOf(
                                                                    "type" to "integer",
                                                                    "minimum" to 0,
                                                                    "maximum" to MAX_RECENT_BUBBLES,
                                                                    "description" to
                                                                            "Maximum number of recent chat bubbles to include."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val options = LookAtOptions.from(arguments)
        val state = stateProvider.currentState(context)
        val response = state.toLookAtResponse(options)
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "context_id" to state.contextId,
                                "agent_id" to state.agentId,
                                "has_visual_snapshot" to (state.visualSnapshot != null)
                        )
        )
    }

    private fun EnvironmentState.toLookAtResponse(options: LookAtOptions): LookAtResponse =
            LookAtResponse(
                    contextId = contextId,
                    agentId = agentId,
                    focus = options.focus,
                    model = model.takeIf { options.includeModel },
                    surface = surface.takeIf { options.includeSurface },
                    lastInteraction = lastInteraction.takeIf { options.includeLastInteraction },
                    visualSnapshot =
                            visualSnapshot.takeIf { options.includeSnapshotMetadata },
                    recentBubbles =
                            if (options.includeRecentBubbles) {
                                recentBubbles.takeLast(options.maxRecentBubbles)
                            } else {
                                null
                            },
                    message = lookAtMessage(options)
            )

    private fun EnvironmentState.lookAtMessage(options: LookAtOptions): String =
            if (options.includeSnapshotMetadata && visualSnapshot != null) {
                "Visual snapshot metadata is available. This tool version does not attach image pixels."
            } else {
                "No visual snapshot metadata is available from this tool result."
            }

    private data class LookAtResponse(
            val contextId: String,
            val agentId: String,
            val focus: String?,
            val model: EnvironmentModelState?,
            val surface: EnvironmentSurfaceState?,
            val lastInteraction: EnvironmentInteraction?,
            val visualSnapshot: EnvironmentVisualSnapshot?,
            val recentBubbles: List<EnvironmentChatBubble>?,
            val message: String
    )

    private data class LookAtOptions(
            val focus: String?,
            val includeModel: Boolean,
            val includeSurface: Boolean,
            val includeLastInteraction: Boolean,
            val includeSnapshotMetadata: Boolean,
            val includeRecentBubbles: Boolean,
            val maxRecentBubbles: Int
    ) {
        companion object {
            fun from(arguments: JsonObject): LookAtOptions =
                    LookAtOptions(
                            focus = arguments.stringOrNull("focus")?.trimOrNull(),
                            includeModel = arguments.booleanOrDefault("include_model", true),
                            includeSurface = arguments.booleanOrDefault("include_surface", true),
                            includeLastInteraction =
                                    arguments.booleanOrDefault("include_last_interaction", true),
                            includeSnapshotMetadata =
                                    arguments.booleanOrDefault(
                                            "include_snapshot_metadata",
                                            true
                                    ),
                            includeRecentBubbles =
                                    arguments.booleanOrDefault("include_recent_bubbles", false),
                            maxRecentBubbles =
                                    arguments.intOrDefault(
                                            name = "max_recent_bubbles",
                                            defaultValue = DEFAULT_MAX_RECENT_BUBBLES,
                                            minimum = 0,
                                            maximum = MAX_RECENT_BUBBLES
                                    )
                    )
        }
    }

    companion object {
        const val NAME: String = "look_at"
        private const val DEFAULT_MAX_RECENT_BUBBLES = 3
        private const val MAX_RECENT_BUBBLES = 20
    }
}

private fun lookAtBooleanParameter(description: String): Map<String, Any> =
        mapOf("type" to "boolean", "description" to description)

private fun JsonObject.stringOrNull(name: String): String? {
    val element = get(name) ?: return null
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isString) {
        "$name must be a string"
    }
    return element.asString
}

private fun JsonObject.booleanOrDefault(name: String, defaultValue: Boolean): Boolean {
    val element = get(name) ?: return defaultValue
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isBoolean) {
        "$name must be a boolean"
    }
    return element.asBoolean
}

private fun JsonObject.intOrDefault(
        name: String,
        defaultValue: Int,
        minimum: Int,
        maximum: Int
): Int {
    val element = get(name) ?: return defaultValue
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isNumber) {
        "$name must be an integer"
    }
    val value = element.asInt
    require(value in minimum..maximum) { "$name must be between $minimum and $maximum" }
    return value
}

private fun String.trimOrNull(): String? = trim().takeIf { it.isNotBlank() }
