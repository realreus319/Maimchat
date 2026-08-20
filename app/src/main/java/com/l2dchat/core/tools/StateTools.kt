package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.storage.RuntimeStateDao

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

        // Impressions are scoped to the single local user per (context, agent). Read-modify-merge
        // against a canonical subject so the planner's partial updates accumulate instead of
        // overwriting prior knowledge, and so the replier can actually read them back.
        val subjectId = canonicalSubjectId(context)
        val impressionId =
                impressionIdFactory?.invoke(context, subjectId)
                        ?: impressionIdFor(context, subjectId)
        val now = clockMillis()
        val existing =
                stateDao.queryImpression(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        subjectId = subjectId
                )
        val content =
                mergeImpressionContent(
                        existing = existing?.content,
                        userId = userId,
                        impressionSummary = impressionSummary,
                        relationshipScore = relationshipScore,
                        relationshipStage = relationshipStage,
                        relationshipSummary = relationshipSummary
                )
        val impression =
                ImpressionEntity(
                        impressionId = impressionId,
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        subjectId = subjectId,
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
                        subjectId = canonicalSubjectId(context)
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

/** Single local user per (context, agent); impressions key on this canonical subject. */
internal fun canonicalSubjectId(context: ToolExecutionContext): String = CANONICAL_SUBJECT_ID

internal const val CANONICAL_SUBJECT_ID: String = "local_user"

/**
 * Read-modify-merge impression content: parse the prior "key: value" lines, overlay only the
 * fields provided in this update, and re-serialize. Fields omitted in the new call keep their
 * previous value instead of being erased.
 */
private fun mergeImpressionContent(
        existing: String?,
        userId: String,
        impressionSummary: String,
        relationshipScore: Double?,
        relationshipStage: String?,
        relationshipSummary: String?
): String {
    val fields = parseImpressionContent(existing).toMutableMap()
    fields["user_id"] = userId
    fields["impression_summary"] = impressionSummary
    relationshipScore?.let { fields["relationship_score"] = it.toString() }
    relationshipStage?.let { fields["relationship_stage"] = it }
    relationshipSummary?.let { fields["relationship_summary"] = it }
    val order =
            listOf(
                    "user_id",
                    "impression_summary",
                    "relationship_score",
                    "relationship_stage",
                    "relationship_summary"
            )
    return (order + fields.keys.filterNot { it in order })
            .mapNotNull { key -> fields[key]?.let { "$key: $it" } }
            .joinToString("\n")
}

private fun parseImpressionContent(content: String?): Map<String, String> {
    if (content.isNullOrBlank()) return emptyMap()
    return content.lineSequence()
            .mapNotNull { line ->
                val index = line.indexOf(':')
                if (index <= 0) return@mapNotNull null
                val key = line.substring(0, index).trim()
                val value = line.substring(index + 1).trim()
                if (key.isBlank() || value.isBlank()) null else key to value
            }
            .toMap()
}

/** Exponential recency decay in [0,1] with a fixed half-life. */
internal fun recencyDecay(timestampMillis: Long, now: Long, halfLifeMillis: Long = MEMORY_HALF_LIFE_MILLIS): Double {
    if (timestampMillis <= 0L || now <= timestampMillis) return 1.0
    val age = (now - timestampMillis).toDouble()
    return Math.pow(0.5, age / halfLifeMillis)
}

private const val MEMORY_HALF_LIFE_MILLIS: Long = 14L * 24 * 60 * 60 * 1000 // 14 days

/**
 * Decay mood valence/arousal toward neutral (0) over time, so a stale emotional spike fades
 * instead of persisting forever between explicit updates. Pure read-time computation.
 */
internal fun decayedMood(
        valence: Double,
        arousal: Double,
        updatedAtMillis: Long,
        now: Long,
        halfLifeMillis: Long = MOOD_HALF_LIFE_MILLIS
): Pair<Double, Double> {
    val factor = recencyDecay(updatedAtMillis, now, halfLifeMillis)
    return valence * factor to arousal * factor
}

private const val MOOD_HALF_LIFE_MILLIS: Long = 6L * 60 * 60 * 1000 // 6 hours

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
