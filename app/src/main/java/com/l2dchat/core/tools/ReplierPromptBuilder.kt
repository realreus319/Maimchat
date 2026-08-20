package com.l2dchat.core.tools

import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart

class ReplierPromptBuilder(
        private val systemPrompt: String = DEFAULT_SYSTEM_PROMPT
) {
    fun buildMessages(request: ReplierTaskRequest): List<LlmMessage> =
            buildMessages(request, ReplierPromptContext())

    fun buildMessages(
            request: ReplierTaskRequest,
            context: ReplierPromptContext
    ): List<LlmMessage> {
        val promptText = buildPromptText(request, context)
        val effectiveSystemPrompt =
                context.systemPrompt?.trim()?.takeIf { it.isNotBlank() } ?: systemPrompt
        val parts = mutableListOf<LlmContentPart>(LlmTextPart(promptText))
        parts.addAll(triggerImageParts(request))
        request.liveImage?.let { parts.add(LlmImageUrlPart(url = it)) }

        return listOf(
                LlmMessage.system(effectiveSystemPrompt),
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

    private fun buildPromptText(
            request: ReplierTaskRequest,
            context: ReplierPromptContext
    ): String {
        val template = context.userPromptTemplate?.trim()?.takeIf { it.isNotBlank() }
        if (template != null) {
            return renderTemplate(template, request, context).trim()
        }
        return buildDefaultPromptText(request, context)
    }

    private fun buildDefaultPromptText(
            request: ReplierTaskRequest,
            context: ReplierPromptContext
    ): String =
            buildString {
                appendLine("Generate the final chat reply from this planner request.")
                appendSection("persona", context.personaPrompt)
                appendSection("current_time", context.currentTimeText)
                appendSection("mood", context.moodState)
                appendSection("impression", context.impressionText)
                appendSection("history", historyText(context))
                appendLine()
                appendLine("[current_trigger]")
                appendLine(request.trigger.toSessionMessage(triggerText(request)).getValue("content"))
                appendSection("planner_thinking", request.thinking)
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

    private fun renderTemplate(
            template: String,
            request: ReplierTaskRequest,
            context: ReplierPromptContext
    ): String {
        val history = historyText(context)
        val guidance = request.replyGuidance?.trim().orEmpty()
        val values =
                mapOf(
                        "persona_prompt" to context.personaPrompt.orEmpty(),
                        "current_time" to context.currentTimeText.orEmpty(),
                        "mood_state" to (context.moodState ?: DEFAULT_MOOD_STATE),
                        "impression_text" to
                                (context.impressionText ?: DEFAULT_IMPRESSION_TEXT),
                        "history_text" to history,
                        "history_section" to
                                history.takeIf { it.isNotBlank() }?.let {
                                    "\n\n最近聊天记录：\n$it\n"
                                }
                                        .orEmpty(),
                        "thinking" to request.thinking.trim(),
                        "thinking_section" to
                                request.thinking.trim().takeIf { it.isNotBlank() }?.let {
                                    "\n\nPlanner的内心判断与已做的事（含借助 AI 智能体得到的结果，仅供你理解当前情况，不要机械复述）：\n$it\n"
                                }
                                        .orEmpty(),
                        "reply_guidance" to guidance,
                        "guidance_section" to
                                guidance.takeIf { it.isNotBlank() }?.let {
                                    "\n补充说明：\n$it\n"
                                }
                                        .orEmpty(),
                        "style_override" to request.styleOverride.orEmpty(),
                        "emotion_hint" to request.emotionHint.orEmpty(),
                        "current_trigger" to
                                request.trigger
                                        .toSessionMessage(triggerText(request))
                                        .getValue("content")
                                        .toString(),
                        "agent_name" to context.agentDisplayName.orEmpty()
                )

        val rendered =
                values.entries.fold(template) { result, (key, value) ->
                    result.replace("{$key}", value)
                }
        // Strip any unresolved {placeholder} tokens a custom template may contain so they are
        // not shipped literally to the model.
        return rendered.replace(UNRESOLVED_PLACEHOLDER_REGEX, "")
    }

    private fun triggerText(request: ReplierTaskRequest): String =
            request.trigger.payload["text"]?.toString()?.takeIf { it.isNotBlank() }
                    .orEmpty()

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

    private fun historyText(context: ReplierPromptContext): String =
            context.historyMessages
                    .mapNotNull { message ->
                        val text = message.text.trim().takeIf { it.isNotBlank() }
                                ?: return@mapNotNull null
                        val sender =
                                if (message.isAssistant) {
                                    val name =
                                            message.senderName?.trim()?.takeIf { it.isNotBlank() }
                                                    ?: context.agentDisplayName
                                                            ?.trim()
                                                            ?.takeIf { it.isNotBlank() }
                                                    ?: DEFAULT_ASSISTANT_NAME
                                    "$name(你)"
                                } else {
                                    message.senderName?.trim()?.takeIf { it.isNotBlank() }
                                            ?: DEFAULT_USER_NAME
                                }
                        "$sender: $text"
                    }
                    .joinToString("\n")

    private fun StringBuilder.appendSection(name: String, value: String?) {
        val normalizedValue = value?.trim()?.takeIf { it.isNotBlank() } ?: return
        appendLine()
        appendLine("[$name]")
        appendLine(normalizedValue)
    }

    companion object {
        const val DEFAULT_SYSTEM_PROMPT: String =
                "你是「小千」，一个外表文静、内心有点小傲娇的女大学生。请把 planner 的请求转化成一条自然的聊天回复，使用用户的语言（默认中文）。说话要简短、平淡，偶尔吐槽或反问，带点小毒舌，不用表情包。除非 planner 明确要求，否则把人设、情绪和 Live2D 提示都隐含在自然语气里。"
        private const val DEFAULT_ASSISTANT_NAME = "助手"
        private const val DEFAULT_USER_NAME = "用户"
        private const val DEFAULT_MOOD_STATE = "暂无心情记录。"
        private const val DEFAULT_IMPRESSION_TEXT = "暂无用户印象记录。"
        private val UNRESOLVED_PLACEHOLDER_REGEX = Regex("""\{[a-z][a-z0-9_]*\}""")
    }
}
