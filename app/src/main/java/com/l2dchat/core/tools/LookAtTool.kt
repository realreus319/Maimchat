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
import java.util.Locale

class LookAtTool(
        private val stateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Inspect current visual environment metadata. Can return an image content block only when a permission-safe direct image reference is already available.",
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
                                                    "include_image" to
                                                            lookAtBooleanParameter(
                                                                    "Whether to include an image content block when the current visual snapshot has a direct data/http(s) image reference. Defaults to false."
                                                            ),
                                                    "image_detail" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "enum" to
                                                                            listOf(
                                                                                    "auto",
                                                                                    "low",
                                                                                    "high"
                                                                            ),
                                                                    "description" to
                                                                            "Optional image detail hint for compatible multimodal providers."
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
        val hasImageContent = response.liveImage != null
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "context_id" to state.contextId,
                                "agent_id" to state.agentId,
                                "has_visual_snapshot" to (state.visualSnapshot != null),
                                "has_image_content" to hasImageContent,
                                "live_image" to response.liveImage
                        )
        )
    }

    private fun EnvironmentState.toLookAtResponse(options: LookAtOptions): LookAtResponse {
        val liveImage = visualSnapshot?.directImageReference()?.takeIf { options.includeImage }
        val message = lookAtMessage(options, liveImage)
        return LookAtResponse(
                contextId = contextId,
                agentId = agentId,
                focus = options.focus,
                model = model.takeIf { options.includeModel },
                surface = surface.takeIf { options.includeSurface },
                lastInteraction = lastInteraction.takeIf { options.includeLastInteraction },
                visualSnapshot = visualSnapshot.takeIf { options.includeSnapshotMetadata },
                recentBubbles =
                        if (options.includeRecentBubbles) {
                            recentBubbles.takeLast(options.maxRecentBubbles)
                        } else {
                            null
                        },
                contentBlocks = lookAtContentBlocks(message, liveImage, options.imageDetail),
                liveImage = liveImage,
                message = message
        )
    }

    private fun EnvironmentState.lookAtMessage(
            options: LookAtOptions,
            liveImage: String?
    ): String =
            when {
                liveImage != null ->
                        "Visual snapshot image content is available in contentBlocks and liveImage."
                options.includeImage && visualSnapshot != null ->
                        "Visual snapshot metadata is available, but no permission-safe direct image content is available."
                options.includeSnapshotMetadata && visualSnapshot != null ->
                        "Visual snapshot metadata is available. Pass include_image=true to request image content when a safe direct image reference exists."
                else -> "No visual snapshot metadata is available from this tool result."
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
            val contentBlocks: List<Map<String, Any>>?,
            val liveImage: String?,
            val message: String
    )

    private data class LookAtOptions(
            val focus: String?,
            val includeModel: Boolean,
            val includeSurface: Boolean,
            val includeLastInteraction: Boolean,
            val includeSnapshotMetadata: Boolean,
            val includeRecentBubbles: Boolean,
            val includeImage: Boolean,
            val imageDetail: String?,
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
                            includeImage = arguments.booleanOrDefault("include_image", false),
                            imageDetail =
                                    arguments.stringOrNull("image_detail")?.let { detail ->
                                        val normalized = detail.trim().lowercase(Locale.ROOT)
                                        require(normalized in IMAGE_DETAIL_VALUES) {
                                            "image_detail must be one of auto, low, high"
                                        }
                                        normalized
                                    },
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
        private val IMAGE_DETAIL_VALUES = setOf("auto", "low", "high")
    }
}

private fun lookAtContentBlocks(
        message: String,
        liveImage: String?,
        imageDetail: String?
): List<Map<String, Any>>? {
    val imageUrl = liveImage ?: return null
    val imagePayload =
            linkedMapOf<String, Any>("url" to imageUrl).apply {
                imageDetail?.let { put("detail", it) }
            }
    return listOf(
            mapOf("type" to "text", "text" to message),
            mapOf("type" to "image_url", "image_url" to imagePayload)
    )
}

private fun EnvironmentVisualSnapshot.directImageReference(): String? {
    val reference = reference.trim().takeIf { it.isNotBlank() } ?: return null
    val lowerReference = reference.lowercase(Locale.ROOT)
    val lowerMimeType = mimeType?.trim()?.lowercase(Locale.ROOT)
    if (lowerMimeType != null && !lowerMimeType.startsWith("image/")) return null
    return when {
        lowerReference.startsWith("data:image/") -> reference
        lowerReference.startsWith("https://") -> reference
        lowerReference.startsWith("http://") -> reference
        else -> null
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
