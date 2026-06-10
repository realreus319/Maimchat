package com.l2dchat.core.storage

import com.google.gson.Gson
import com.l2dchat.core.message.PlannerMessageEntity
import com.l2dchat.core.message.PlannerRoundEntity
import com.l2dchat.core.reply.PlannerRoundRecord
import com.l2dchat.core.reply.PlannerSessionMessageRecord
import com.l2dchat.core.reply.PlannerSessionStore

class RoomPlannerSessionStore(
        private val plannerStateDao: PlannerStateDao,
        private val gson: Gson = Gson()
) : PlannerSessionStore {
    override suspend fun upsertRound(round: PlannerRoundRecord) {
        plannerStateDao.appendPlannerRound(
                PlannerRoundEntity(
                        roundId = round.roundId,
                        contextId = round.contextId,
                        agentId = round.agentId,
                        triggerMessageId = round.triggerMessageId,
                        state = round.state,
                        createdAtMillis = round.createdAtMillis,
                        updatedAtMillis = round.updatedAtMillis
                )
        )
    }

    override suspend fun appendMessage(message: PlannerSessionMessageRecord) {
        plannerStateDao.appendPlannerMessage(
                PlannerMessageEntity(
                        plannerMessageId = message.plannerMessageId,
                        roundId = message.roundId,
                        sequence = message.sequence,
                        role = message.role,
                        content = message.content,
                        toolCallId = message.toolCallId,
                        payloadJson = message.payload?.let { gson.toJson(it) },
                        createdAtMillis = message.createdAtMillis
                )
        )
    }
}
