package com.l2dchat.core.reply

data class PlannerRoundRecord(
        val roundId: String,
        val contextId: String,
        val agentId: String,
        val triggerMessageId: String?,
        val state: String,
        val createdAtMillis: Long,
        val updatedAtMillis: Long
)

data class PlannerSessionMessageRecord(
        val plannerMessageId: String,
        val roundId: String,
        val sequence: Int,
        val role: String,
        val content: String?,
        val toolCallId: String? = null,
        val payload: Map<String, Any?>? = null,
        val createdAtMillis: Long
)

interface PlannerSessionStore {
    suspend fun upsertRound(round: PlannerRoundRecord)

    suspend fun appendMessage(message: PlannerSessionMessageRecord)
}

object NoopPlannerSessionStore : PlannerSessionStore {
    override suspend fun upsertRound(round: PlannerRoundRecord) = Unit

    override suspend fun appendMessage(message: PlannerSessionMessageRecord) = Unit
}

object PlannerSessionState {
    const val GENERATING = "generating"
    const val COMPLETED = "completed"
    const val CANCELLED = "cancelled"
    const val FAILED = "failed"
}

object PlannerSessionRole {
    const val TRIGGER = "trigger"
    const val USER = "user"
    const val ASSISTANT = "assistant"
    const val TOOL = "tool"
}
