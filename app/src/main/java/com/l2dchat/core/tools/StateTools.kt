package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.storage.RuntimeStateDao
import java.util.Locale

class MemoryStoreTool(
        private val stateDao: RuntimeStateDao,
        private val gson: Gson = Gson(),
        private val clockMillis: () -> Long = { System.currentTimeMillis() },
        private val memoryIdFactory: ((ToolExecutionContext, String, Long) -> String)? = null
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Store a durable local memory for the current chat context and agent.",
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
                                                                            "Memory text to preserve for future replies."
                                                            ),
                                                    "category" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "enum" to MEMORY_CATEGORIES,
                                                                    "description" to
                                                                            "Optional memory category. Defaults to general."
                                                            ),
                                                    "importance" to
                                                            mapOf(
                                                                    "type" to "number",
                                                                    "minimum" to 0.0,
                                                                    "maximum" to 1.0,
                                                                    "description" to
                                                                            "Importance from 0.0 to 1.0. Defaults to 0.5."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val content = arguments.requiredTrimmedString("content", "memory_store.content")
        val category =
                arguments.stringOrNull("category")
                        ?.trim()
                        ?.lowercase(Locale.ROOT)
                        ?.takeIf { it.isNotBlank() }
                        ?: DEFAULT_CATEGORY
        require(category in MEMORY_CATEGORIES) {
            "memory_store.category must be one of ${MEMORY_CATEGORIES.joinToString()}"
        }
        val importance =
                arguments.doubleOrDefault("importance", DEFAULT_IMPORTANCE, 0.0, 1.0)
        val now = clockMillis()
        val memoryId =
                memoryIdFactory?.invoke(context, content, now)
                        ?: memoryIdFor(context, content, now)
        val memory =
                MemoryEntity(
                        memoryId = memoryId,
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        content = content,
                        importance = importance,
                        createdAtMillis = now,
                        updatedAtMillis = now
                )
        stateDao.upsertMemory(memory)
        val response =
                MemoryStoreResponse(
                        stored = true,
                        memoryId = memoryId,
                        category = category,
                        importance = importance,
                        contentPreview = content.take(PREVIEW_LIMIT)
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "memory_id" to memoryId,
                                "category" to category,
                                "importance" to importance
                        )
        )
    }

    private data class MemoryStoreResponse(
            val stored: Boolean,
            val memoryId: String,
            val category: String,
            val importance: Double,
            val contentPreview: String
    )

    companion object {
        const val NAME: String = "memory_store"
        private const val DEFAULT_CATEGORY = "general"
        private const val DEFAULT_IMPORTANCE = 0.5
        private const val PREVIEW_LIMIT = 120
        private val MEMORY_CATEGORIES =
                listOf("general", "conversation", "fact", "preference", "event")
    }
}

class MemorySearchTool(
        private val stateDao: RuntimeStateDao,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Search durable local memories for the current chat context and agent.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "required" to listOf("query"),
                                    "properties" to
                                            mapOf(
                                                    "query" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Keyword query to search in memory content."
                                                            ),
                                                    "top_k" to
                                                            mapOf(
                                                                    "type" to "integer",
                                                                    "minimum" to 1,
                                                                    "maximum" to MAX_TOP_K,
                                                                    "description" to
                                                                            "Maximum number of memories to return. Defaults to 5."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val query = arguments.requiredTrimmedString("query", "memory_search.query")
        val topK = arguments.intOrDefault("top_k", DEFAULT_TOP_K, 1, MAX_TOP_K)
        val memories =
                stateDao.searchMemories(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        query = query.escapeSqlLikePattern(),
                        limit = topK
                )
        val response =
                MemorySearchResponse(
                        query = query,
                        total = memories.size,
                        results = memories.map { it.toMemorySearchItem() }
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "query" to query,
                                "result_count" to memories.size
                        )
        )
    }

    private data class MemorySearchResponse(
            val query: String,
            val total: Int,
            val results: List<MemorySearchItem>
    )

    private data class MemorySearchItem(
            val memoryId: String,
            val content: String,
            val importance: Double,
            val updatedAtMillis: Long
    )

    private fun MemoryEntity.toMemorySearchItem(): MemorySearchItem =
            MemorySearchItem(
                    memoryId = memoryId,
                    content = content,
                    importance = importance,
                    updatedAtMillis = updatedAtMillis
            )

    companion object {
        const val NAME: String = "memory_search"
        private const val DEFAULT_TOP_K = 5
        private const val MAX_TOP_K = 20
    }
}

class GetUserImpressionsTool(
        private val stateDao: RuntimeStateDao,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Return recently updated local user impressions for the current context and agent.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "properties" to emptyMap<String, Any>()
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val impressions =
                stateDao.queryImpressions(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        limit = DEFAULT_LIMIT
                )
        val response =
                ImpressionListResponse(
                        total = impressions.size,
                        impressions = impressions.map { it.toImpressionItem() }
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "result_count" to impressions.size
                        )
        )
    }

    private data class ImpressionListResponse(
            val total: Int,
            val impressions: List<ImpressionItem>
    )

    companion object {
        const val NAME: String = "tool_get_user_impressions"
        private const val DEFAULT_LIMIT = 20
    }
}

class UpdateUserImpressionTool(
        private val stateDao: RuntimeStateDao,
        private val gson: Gson = Gson(),
        private val clockMillis: () -> Long = { System.currentTimeMillis() },
        private val impressionIdFactory: ((ToolExecutionContext, String) -> String)? = null
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Create or update a local impression summary for a specific user.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "required" to listOf("user_id", "impression_summary"),
                                    "properties" to
                                            mapOf(
                                                    "user_id" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "User id whose impression should be updated."
                                                            ),
                                                    "impression_summary" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Concise impression summary."
                                                            ),
                                                    "relationship_score" to
                                                            mapOf(
                                                                    "type" to "number",
                                                                    "description" to
                                                                            "Optional relationship score from the planner."
                                                            ),
                                                    "relationship_stage" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional relationship stage label."
                                                            ),
                                                    "relationship_summary" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional relationship summary."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val userId =
                arguments.requiredTrimmedString(
                        "user_id",
                        "tool_update_user_impression.user_id"
                )
        val impressionSummary =
                arguments.requiredTrimmedString(
                        "impression_summary",
                        "tool_update_user_impression.impression_summary"
                )
        val relationshipScore = arguments.doubleOrNull("relationship_score")
        val relationshipStage = arguments.stringOrNull("relationship_stage")?.trimOrNull()
        val relationshipSummary = arguments.stringOrNull("relationship_summary")?.trimOrNull()
        val content =
                buildImpressionContent(
                        impressionSummary = impressionSummary,
                        relationshipScore = relationshipScore,
                        relationshipStage = relationshipStage,
                        relationshipSummary = relationshipSummary
                )
        val impressionId =
                impressionIdFactory?.invoke(context, userId) ?: impressionIdFor(context, userId)
        val now = clockMillis()
        val impression =
                ImpressionEntity(
                        impressionId = impressionId,
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        subjectId = userId,
                        content = content,
                        updatedAtMillis = now
                )
        stateDao.upsertImpression(impression)
        val response =
                ImpressionUpdateResponse(
                        impressionUpdated = true,
                        userId = userId,
                        impressionId = impressionId,
                        content = content
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "user_id" to userId,
                                "impression_id" to impressionId
                        )
        )
    }

    private data class ImpressionUpdateResponse(
            val impressionUpdated: Boolean,
            val userId: String,
            val impressionId: String,
            val content: String
    )

    companion object {
        const val NAME: String = "tool_update_user_impression"
    }
}

class QueryImpressionTool(
        private val stateDao: RuntimeStateDao,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description = "Query the local impression summary for a specific user.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "required" to listOf("user_id"),
                                    "properties" to
                                            mapOf(
                                                    "user_id" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "User id whose impression should be queried."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val userId = arguments.requiredTrimmedString("user_id", "tool_query_impression.user_id")
        val impression =
                stateDao.queryImpression(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        subjectId = userId
                )
        val response =
                ImpressionQueryResponse(
                        found = impression != null,
                        userId = userId,
                        impression = impression?.toImpressionItem()
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "user_id" to userId,
                                "found" to (impression != null)
                        )
        )
    }

    private data class ImpressionQueryResponse(
            val found: Boolean,
            val userId: String,
            val impression: ImpressionItem?
    )

    companion object {
        const val NAME: String = "tool_query_impression"
    }
}

class UpdateMoodStateTool(
        private val stateDao: RuntimeStateDao,
        private val gson: Gson = Gson(),
        private val clockMillis: () -> Long = { System.currentTimeMillis() }
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Update the local mood state for the current context and agent.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "required" to listOf("valence", "arousal"),
                                    "properties" to
                                            mapOf(
                                                    "valence" to
                                                            mapOf(
                                                                    "type" to "number",
                                                                    "minimum" to -1.0,
                                                                    "maximum" to 1.0,
                                                                    "description" to
                                                                            "Mood valence from -1.0 to 1.0."
                                                            ),
                                                    "arousal" to
                                                            mapOf(
                                                                    "type" to "number",
                                                                    "minimum" to 0.0,
                                                                    "maximum" to 1.0,
                                                                    "description" to
                                                                            "Mood arousal from 0.0 to 1.0."
                                                            ),
                                                    "state" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "Optional human-readable mood state text."
                                                            ),
                                                    "state_json" to
                                                            mapOf(
                                                                    "type" to "object",
                                                                    "description" to
                                                                            "Optional structured mood state object."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val valence = arguments.requiredDouble("valence", -1.0, 1.0)
        val arousal = arguments.requiredDouble("arousal", 0.0, 1.0)
        val stateText =
                arguments.objectJsonOrNull("state_json")
                        ?: arguments.stringOrNull("state")?.trimOrNull()
        val now = clockMillis()
        val moodState =
                MoodStateEntity(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        valence = valence,
                        arousal = arousal,
                        stateJson = stateText,
                        updatedAtMillis = now
                )
        stateDao.upsertMoodState(moodState)
        val response =
                MoodUpdateResponse(
                        moodUpdated = true,
                        valence = valence,
                        arousal = arousal,
                        state = stateText,
                        updatedAtMillis = now
                )
        return ToolExecutionResult(
                llmContent = gson.toJson(response),
                metadata =
                        mapOf(
                                "tool" to NAME,
                                "valence" to valence,
                                "arousal" to arousal
                        )
        )
    }

    private data class MoodUpdateResponse(
            val moodUpdated: Boolean,
            val valence: Double,
            val arousal: Double,
            val state: String?,
            val updatedAtMillis: Long
    )

    companion object {
        const val NAME: String = "tool_update_mood_state"
    }
}

private data class ImpressionItem(
        val userId: String,
        val content: String,
        val updatedAtMillis: Long
)

private fun ImpressionEntity.toImpressionItem(): ImpressionItem =
        ImpressionItem(
                userId = subjectId,
                content = content,
                updatedAtMillis = updatedAtMillis
        )

private fun buildImpressionContent(
        impressionSummary: String,
        relationshipScore: Double?,
        relationshipStage: String?,
        relationshipSummary: String?
): String {
    val lines = mutableListOf("impression_summary: $impressionSummary")
    relationshipScore?.let { lines += "relationship_score: $it" }
    relationshipStage?.let { lines += "relationship_stage: $it" }
    relationshipSummary?.let { lines += "relationship_summary: $it" }
    return lines.joinToString("\n")
}

private fun memoryIdFor(
        context: ToolExecutionContext,
        content: String,
        timestampMillis: Long
): String =
        "memory_" +
                listOf(
                                context.routingKey.contextId,
                                context.routingKey.agentId,
                                timestampMillis.toString(),
                                Integer.toHexString(content.hashCode())
                        )
                        .joinToString("_") { it.stateToolIdPart() }

private fun impressionIdFor(context: ToolExecutionContext, userId: String): String =
        "impression_" +
                listOf(context.routingKey.contextId, context.routingKey.agentId, userId)
                        .joinToString("_") { it.stateToolIdPart() }

private fun String.stateToolIdPart(): String =
        trim()
                .ifBlank { "unknown" }
                .replace(Regex("[^A-Za-z0-9_.:-]+"), "_")

private fun JsonObject.requiredTrimmedString(name: String, label: String): String {
    val value = stringOrNull(name)?.trim().orEmpty()
    require(value.isNotBlank()) { "$label must not be blank" }
    return value
}

private fun JsonObject.stringOrNull(name: String): String? {
    val element = get(name) ?: return null
    require(!element.isJsonNull && element.isJsonPrimitive && element.asJsonPrimitive.isString) {
        "$name must be a string"
    }
    return element.asString
}

private fun JsonObject.doubleOrNull(name: String): Double? {
    val primitive = get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asJsonPrimitive
            ?: return null
    require(primitive.isNumber) { "$name must be a number" }
    return primitive.asDouble
}

private fun JsonObject.doubleOrDefault(
        name: String,
        defaultValue: Double,
        minimum: Double,
        maximum: Double
): Double {
    val value = doubleOrNull(name) ?: return defaultValue
    require(value in minimum..maximum) { "$name must be between $minimum and $maximum" }
    return value
}

private fun JsonObject.requiredDouble(
        name: String,
        minimum: Double,
        maximum: Double
): Double {
    val value = doubleOrNull(name)
    require(value != null) { "$name is required" }
    require(value in minimum..maximum) { "$name must be between $minimum and $maximum" }
    return value
}

private fun JsonObject.intOrDefault(
        name: String,
        defaultValue: Int,
        minimum: Int,
        maximum: Int
): Int {
    val primitive = get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asJsonPrimitive
            ?: return defaultValue
    require(primitive.isNumber) { "$name must be an integer" }
    val value = primitive.asInt
    require(value in minimum..maximum) { "$name must be between $minimum and $maximum" }
    return value
}

private fun JsonObject.objectJsonOrNull(name: String): String? {
    val element = get(name) ?: return null
    require(!element.isJsonNull && element.isJsonObject) { "$name must be an object" }
    return element.asJsonObject.toString().trim().takeIf { it.isNotBlank() }
}

private fun String.trimOrNull(): String? = trim().takeIf { it.isNotBlank() }

private fun String.escapeSqlLikePattern(): String =
        buildString {
            for (character in this@escapeSqlLikePattern) {
                if (character == '\\' || character == '%' || character == '_') {
                    append('\\')
                }
                append(character)
            }
        }
