package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentChatBubble
import com.l2dchat.core.environment.EnvironmentInteraction
import com.l2dchat.core.environment.EnvironmentModelState
import com.l2dchat.core.environment.EnvironmentMotion
import com.l2dchat.core.environment.EnvironmentState
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentSurfaceState
import com.l2dchat.core.environment.EnvironmentVisualSnapshot
import com.l2dchat.core.llm.LlmToolDefinition

class GetWorldStateTool(
        private val stateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Return the current Android app, Live2D, wallpaper, interaction, and recent chat environment state for planning.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "properties" to
                                            mapOf(
                                                    "include_model" to
                                                            booleanParameter(
                                                                    "Whether to include the selected Live2D model state. Defaults to true."
                                                            ),
                                                    "include_motions" to
                                                            booleanParameter(
                                                                    "Whether to include available Live2D motions. Defaults to true."
                                                            ),
                                                    "max_motions" to
                                                            boundedIntegerParameter(
                                                                    "Maximum number of motions to include.",
                                                                    minimum = 0,
                                                                    maximum = MAX_MOTIONS
                                                            ),
                                                    "include_surface" to
                                                            booleanParameter(
                                                                    "Whether to include app, wallpaper, and background surface state. Defaults to true."
                                                            ),
                                                    "include_last_interaction" to
                                                            booleanParameter(
                                                                    "Whether to include the latest touch, drag, or environment interaction. Defaults to true."
                                                            ),
                                                    "include_recent_bubbles" to
                                                            booleanParameter(
                                                                    "Whether to include recent visible chat bubbles. Defaults to true."
                                                            ),
                                                    "max_recent_bubbles" to
                                                            boundedIntegerParameter(
                                                                    "Maximum number of recent chat bubbles to include.",
                                                                    minimum = 0,
                                                                    maximum = MAX_RECENT_BUBBLES
                                                            ),
                                                    "include_visual_snapshot" to
                                                            booleanParameter(
                                                                    "Whether to include visual snapshot metadata when one is available. Defaults to true."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val options = WorldStateOptions.from(arguments)
        val state = stateProvider.currentState(context)
        val response = state.toToolResponse(options)
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "context_id" to state.contextId,
                                "agent_id" to state.agentId
                        )
        )
    }

    private fun EnvironmentState.toToolResponse(options: WorldStateOptions): WorldStateToolResponse =
            WorldStateToolResponse(
                    contextId = contextId,
                    agentId = agentId,
                    model = model.takeIf { options.includeModel },
                    motions =
                            if (options.includeMotions) {
                                motions.take(options.maxMotions)
                            } else {
                                null
                            },
                    expression = expression,
                    surface = surface.takeIf { options.includeSurface },
                    lastInteraction = lastInteraction.takeIf { options.includeLastInteraction },
                    recentBubbles =
                            if (options.includeRecentBubbles) {
                                recentBubbles.takeLast(options.maxRecentBubbles)
                            } else {
                                null
                            },
                    visualSnapshot = visualSnapshot.takeIf { options.includeVisualSnapshot },
                    metadata = metadata.takeIf { it.isNotEmpty() }
            )

    private data class WorldStateToolResponse(
            val contextId: String,
            val agentId: String,
            val model: EnvironmentModelState?,
            val motions: List<EnvironmentMotion>?,
            val expression: String?,
            val surface: EnvironmentSurfaceState?,
            val lastInteraction: EnvironmentInteraction?,
            val recentBubbles: List<EnvironmentChatBubble>?,
            val visualSnapshot: EnvironmentVisualSnapshot?,
            val metadata: Map<String, Any?>?
    )

    private data class WorldStateOptions(
            val includeModel: Boolean,
            val includeMotions: Boolean,
            val maxMotions: Int,
            val includeSurface: Boolean,
            val includeLastInteraction: Boolean,
            val includeRecentBubbles: Boolean,
            val maxRecentBubbles: Int,
            val includeVisualSnapshot: Boolean
    ) {
        companion object {
            fun from(arguments: JsonObject): WorldStateOptions =
                    WorldStateOptions(
                            includeModel = arguments.booleanOrDefault("include_model", true),
                            includeMotions = arguments.booleanOrDefault("include_motions", true),
                            maxMotions =
                                    arguments.intOrDefault(
                                            name = "max_motions",
                                            defaultValue = DEFAULT_MAX_MOTIONS,
                                            minimum = 0,
                                            maximum = MAX_MOTIONS
                                    ),
                            includeSurface = arguments.booleanOrDefault("include_surface", true),
                            includeLastInteraction =
                                    arguments.booleanOrDefault("include_last_interaction", true),
                            includeRecentBubbles =
                                    arguments.booleanOrDefault("include_recent_bubbles", true),
                            maxRecentBubbles =
                                    arguments.intOrDefault(
                                            name = "max_recent_bubbles",
                                            defaultValue = DEFAULT_MAX_RECENT_BUBBLES,
                                            minimum = 0,
                                            maximum = MAX_RECENT_BUBBLES
                                    ),
                            includeVisualSnapshot =
                                    arguments.booleanOrDefault("include_visual_snapshot", true)
                    )
        }
    }

    companion object {
        const val NAME: String = "get_world_state"
        private const val DEFAULT_MAX_MOTIONS = 50
        private const val MAX_MOTIONS = 200
        private const val DEFAULT_MAX_RECENT_BUBBLES = 10
        private const val MAX_RECENT_BUBBLES = 50
    }
}

private fun booleanParameter(description: String): Map<String, Any> =
        mapOf("type" to "boolean", "description" to description)

private fun boundedIntegerParameter(
        description: String,
        minimum: Int,
        maximum: Int
): Map<String, Any> =
        mapOf(
                "type" to "integer",
                "minimum" to minimum,
                "maximum" to maximum,
                "description" to description
        )

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
