package com.l2dchat.core.storage

import com.l2dchat.core.reply.PlannerPromptContextBlock
import com.l2dchat.core.reply.PlannerPromptContextProvider
import com.l2dchat.core.reply.PlannerTurnContext
import com.l2dchat.core.tools.CANONICAL_SUBJECT_ID
import com.l2dchat.core.tools.decayedMood
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Surfaces durable Room-backed state (the user impression, current mood) into the planner prompt
 * so the decision-maker actually reasons with memory — not only the replier wording stage.
 */
class RoomPlannerPromptContextProvider(
        private val stateDao: RuntimeStateDao,
        private val clockMillis: () -> Long = { System.currentTimeMillis() }
) : PlannerPromptContextProvider {
    override suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock> {
        val contextId = context.routingKey.contextId
        val agentId = context.routingKey.agentId
        val now = clockMillis()
        val blocks = mutableListOf<PlannerPromptContextBlock>()

        // Anchor the decision-maker in real time. Without this the planner never knows today's date, so
        // "最新/今年/现在" queries fall back to the model's stale training knowledge. (The replier already
        // gets current_time; the planner — which actually decides whether to research — did not.)
        blocks +=
                PlannerPromptContextBlock(
                        name = "current_time",
                        content = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.ROOT).format(Date(now))
                )

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
}
