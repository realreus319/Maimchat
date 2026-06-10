package com.l2dchat.core.storage

import com.l2dchat.core.reply.PlannerSystemPromptProvider
import com.l2dchat.core.reply.PlannerTurnContext

class RoomPlannerSystemPromptProvider(
        private val stateDao: RuntimeStateDao,
        private val templateName: String
) : PlannerSystemPromptProvider {
    override suspend fun systemPromptFor(context: PlannerTurnContext): String? =
            stateDao
                    .queryPromptTemplate(templateName, context.routingKey.agentId)
                    ?.body
                    ?.trim()
                    ?.takeIf { it.isNotBlank() }

    companion object {
        const val PLANNER_SYSTEM_TEMPLATE = "planner_system"
        const val DECISION_SYSTEM_TEMPLATE = "decision_system"
    }
}
