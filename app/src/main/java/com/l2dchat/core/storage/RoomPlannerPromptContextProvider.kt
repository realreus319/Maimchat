package com.l2dchat.core.storage

import com.l2dchat.core.reply.PlannerPromptContextBlock
import com.l2dchat.core.reply.PlannerPromptContextProvider
import com.l2dchat.core.reply.PlannerTurnContext
import com.l2dchat.core.tools.CANONICAL_SUBJECT_ID
import com.l2dchat.core.tools.decayedMood
import java.util.Locale

/**
 * Surfaces durable Room-backed state (top memories, the user impression, current mood) into the
 * planner prompt so the decision-maker actually reasons with memory — not only the replier
 * wording stage. Memories are ordered by importance/recency (the same signal used for eviction).
 */
class RoomPlannerPromptContextProvider(
        private val stateDao: RuntimeStateDao,
        private val memoryLimit: Int = DEFAULT_MEMORY_LIMIT,
        private val clockMillis: () -> Long = { System.currentTimeMillis() }
) : PlannerPromptContextProvider {
    override suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock> {
        val contextId = context.routingKey.contextId
        val agentId = context.routingKey.agentId
        val now = clockMillis()
        val blocks = mutableListOf<PlannerPromptContextBlock>()

        val memoryText =
                stateDao.queryMemories(contextId, agentId, memoryLimit)
                        .mapNotNull { it.content.trim().takeIf { c -> c.isNotBlank() } }
                        .joinToString("\n") { "- $it" }
        if (memoryText.isNotBlank()) {
            blocks += PlannerPromptContextBlock(name = "memory", content = memoryText)
        }

        stateDao.queryImpression(contextId, agentId, CANONICAL_SUBJECT_ID)
                ?.content
                ?.trim()
                ?.takeIf { it.isNotBlank() }
                ?.let { blocks += PlannerPromptContextBlock(name = "impression", content = it) }

        stateDao.queryMoodState(contextId, agentId)?.let { mood ->
            val (valence, arousal) =
                    decayedMood(mood.valence, mood.arousal, mood.updatedAtMillis, now)
            val moodText =
                    mood.stateJson?.trim()?.takeIf { it.isNotBlank() }
                            ?: if (kotlin.math.abs(valence) >= 0.05 || arousal >= 0.05) {
                                "valence=${valence.fmt()}, arousal=${arousal.fmt()}"
                            } else {
                                null
                            }
            moodText?.let { blocks += PlannerPromptContextBlock(name = "mood", content = it) }
        }

        return blocks
    }

    private fun Double.fmt(): String = String.format(Locale.ROOT, "%.2f", this)

    companion object {
        private const val DEFAULT_MEMORY_LIMIT = 6
    }
}
