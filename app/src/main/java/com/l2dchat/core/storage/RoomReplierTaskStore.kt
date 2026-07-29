package com.l2dchat.core.storage

import com.google.gson.Gson
import com.l2dchat.core.message.ToolTaskEntity
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskSnapshot
import com.l2dchat.core.tools.ReplierTaskStore
import com.l2dchat.core.tools.ReplierTool

class RoomReplierTaskStore(
        private val plannerStateDao: PlannerStateDao,
        private val gson: Gson = Gson()
) : ReplierTaskStore {
    override suspend fun upsertTaskSnapshot(
            request: ReplierTaskRequest,
            snapshot: ReplierTaskSnapshot,
            createdAtMillis: Long,
            updatedAtMillis: Long
    ) {
        plannerStateDao.appendToolTask(
                ToolTaskEntity(
                        taskId = request.taskId,
                        roundId = request.roundId,
                        toolName = ReplierTool.NAME,
                        state = snapshot.state.name,
                        inputJson = gson.toJson(request.toInputPayload()),
                        outputJson = snapshot.toOutputPayload()?.let { gson.toJson(it) },
                        error = snapshot.errorMessage,
                        createdAtMillis = createdAtMillis,
                        updatedAtMillis = updatedAtMillis
                )
        )
    }

    private fun ReplierTaskRequest.toInputPayload(): Map<String, Any?> =
            linkedMapOf(
                    "routing_key" to
                            linkedMapOf(
                                    "context_id" to routingKey.contextId,
                                    "agent_id" to routingKey.agentId
                            ),
                    "trigger_message_id" to trigger.messageId,
                    "trigger_type" to trigger.triggerType.wireValue,
                    "thinking" to thinking,
                    "reply_guidance" to replyGuidance,
                    "style_override" to styleOverride,
                    "emotion_hint" to emotionHint,
                    "is_progress_update" to isProgressUpdate,
                    "include_action" to includeAction,
                    "live_image" to liveImage
            )

    private fun ReplierTaskSnapshot.toOutputPayload(): Map<String, Any?>? {
        if (previewText.isBlank() && replyText.isNullOrBlank() && !backgrounded) {
            return null
        }
        return linkedMapOf(
                "preview_text" to previewText.takeIf { it.isNotBlank() },
                "reply_text" to replyText,
                "backgrounded" to backgrounded
        )
    }
}
