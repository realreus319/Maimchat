package com.l2dchat.core.tools

import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart

class ReplierPromptBuilder(
        private val systemPrompt: String = DEFAULT_SYSTEM_PROMPT
) {
    fun buildMessages(request: ReplierTaskRequest): List<LlmMessage> {
        val promptText = buildPromptText(request)
        val parts = mutableListOf<LlmContentPart>(LlmTextPart(promptText))
        parts.addAll(triggerImageParts(request))
        request.liveImage?.let { parts.add(LlmImageUrlPart(url = it)) }

        return listOf(
                LlmMessage.system(systemPrompt),
                LlmMessage(
                        role = LlmMessageRole.USER,
                        content = parts,
                        metadata =
                                mapOf(
                                        "task_id" to request.taskId,
                                        "context_id" to request.routingKey.contextId,
                                        "agent_id" to request.routingKey.agentId,
                                        "trigger_type" to request.trigger.triggerType.wireValue,
                                        "message_id" to request.trigger.messageId,
                                        "is_progress_update" to request.isProgressUpdate,
                                        "include_action" to request.includeAction
                                )
                )
        )
    }

    private fun buildPromptText(request: ReplierTaskRequest): String =
            buildString {
                appendLine("Generate the final chat reply from this planner request.")
                appendLine()
                appendLine("[current_trigger]")
                appendLine(request.trigger.toSessionMessage(triggerText(request)).getValue("content"))
                appendLine()
                appendLine("[planner_content]")
                appendLine(request.content.trim())
                appendSection("reply_guidance", request.replyGuidance)
                appendSection("style_override", request.styleOverride)
                appendSection("emotion_hint", request.emotionHint)
                if (request.isProgressUpdate) {
                    appendLine()
                    appendLine("[progress_update]")
                    appendLine("This reply is a progress update. Keep it brief and do not pretend the task is finished.")
                }
                if (request.includeAction) {
                    appendLine()
                    appendLine("[include_action]")
                    appendLine("If a Live2D action or expression is clearly implied, include it naturally in the reply text.")
                }
                if (request.liveImage != null) {
                    appendLine()
                    appendLine("[live_image]")
                    appendLine("A current visual frame is attached as image input.")
                }
                appendLine()
                appendLine(
                        "Return only the final reply text. Do not include analysis, JSON, tool calls, or labels."
                )
            }.trim()

    private fun triggerText(request: ReplierTaskRequest): String =
            request.trigger.payload["text"]?.toString()?.takeIf { it.isNotBlank() }
                    ?: request.content

    private fun triggerImageParts(request: ReplierTaskRequest): List<LlmImageUrlPart> {
        @Suppress("UNCHECKED_CAST")
        val blocks = request.trigger.payload["content_blocks"] as? List<Map<String, Any?>> ?: return emptyList()
        return blocks.mapNotNull { block ->
            if (block["type"]?.toString() != "image_url") {
                return@mapNotNull null
            }
            imageUrlFrom(block["image_url"])
        }
    }

    private fun imageUrlFrom(value: Any?): LlmImageUrlPart? {
        @Suppress("UNCHECKED_CAST")
        val imageMap = value as? Map<String, Any?>
        val url = imageMap?.get("url")?.toString() ?: value?.toString()
        val normalizedUrl = url?.takeIf { it.isNotBlank() } ?: return null
        return LlmImageUrlPart(
                url = normalizedUrl,
                detail = imageMap?.get("detail")?.toString()?.takeIf { it.isNotBlank() }
        )
    }

    private fun StringBuilder.appendSection(name: String, value: String?) {
        val normalizedValue = value?.trim()?.takeIf { it.isNotBlank() } ?: return
        appendLine()
        appendLine("[$name]")
        appendLine(normalizedValue)
    }

    companion object {
        const val DEFAULT_SYSTEM_PROMPT: String =
                "You are Maimchat's local replier. Turn the planner request into a natural chat reply in the user's language. Keep persona, emotion, and Live2D hints implicit unless the planner explicitly asks for them."
    }
}
