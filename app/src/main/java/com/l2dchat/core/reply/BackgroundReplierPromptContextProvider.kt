package com.l2dchat.core.reply

import com.l2dchat.core.tools.ReplierTaskManager
import com.l2dchat.core.tools.ReplierTaskSummary

class BackgroundReplierPromptContextProvider(
        private val taskManager: ReplierTaskManager,
        private val maxTasks: Int = 8,
        private val maxTextChars: Int = 240
) : PlannerPromptContextProvider {
    init {
        require(maxTasks > 0) { "maxTasks must be positive" }
        require(maxTextChars > 0) { "maxTextChars must be positive" }
    }

    override suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock> {
        val summaries =
                taskManager
                        .backgroundTaskSummaries(context.routingKey)
                        .take(maxTasks)
        if (summaries.isEmpty()) {
            return emptyList()
        }
        return listOf(
                PlannerPromptContextBlock(
                        name = "background_replier_tasks",
                        content = summaries.joinToString(separator = "\n\n") { it.toPromptText() }
                )
        )
    }

    private fun ReplierTaskSummary.toPromptText(): String =
            buildString {
                append("- task_id: ")
                append(taskId)
                append('\n')
                append("  state: ")
                append(state.name)
                append('\n')
                append("  trigger_message_id: ")
                append(triggerMessageId)
                append('\n')
                append("  trigger_text: ")
                append(triggerText.inlinePreview())
                append('\n')
                append("  preview: ")
                append((replyText ?: previewText).inlinePreview())
                errorMessage?.takeIf { it.isNotBlank() }?.let {
                    append('\n')
                    append("  error: ")
                    append(it.inlinePreview())
                }
            }

    private fun String.inlinePreview(): String {
        val normalized = replace(Regex("\\s+"), " ").trim()
        val safeText = normalized.ifBlank { "(empty)" }
        return if (safeText.length <= maxTextChars) {
            safeText
        } else {
            safeText.take(maxTextChars).trimEnd() + "..."
        }
    }
}
