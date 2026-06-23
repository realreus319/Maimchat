package com.l2dchat.core.reply

import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.trigger.Trigger

class PlannerPromptBuilder(
        private val systemPrompt: String = DEFAULT_SYSTEM_PROMPT,
        private val contextProvider: PlannerPromptContextProvider = EmptyPlannerPromptContextProvider
) {
    suspend fun buildMessages(context: PlannerTurnContext): List<LlmMessage> =
            buildMessages(context = context, systemPromptOverride = null)

    suspend fun buildMessages(
            context: PlannerTurnContext,
            systemPromptOverride: String?
    ): List<LlmMessage> {
        val sessionMessage = context.trigger.toSessionMessage(context.triggerText)
        val userText = sessionMessage["content"]?.toString().orEmpty()
        val promptContextPart = promptContextPart(contextProvider.blocksFor(context))
        val userParts =
                listOfNotNull(promptContextPart) +
                        contentPartsFrom(sessionMessage["content_blocks"], fallbackText = userText)
        val metadata =
                sessionMessage.filterKeys {
                    it != "role" && it != "content" && it != "content_blocks"
                }
        return listOf(
                LlmMessage.system(systemPromptOverride.trimmedOrNull() ?: systemPrompt),
                LlmMessage(
                        role = LlmMessageRole.USER,
                        content = userParts,
                        metadata = metadata
                )
        )
    }

    private fun promptContextPart(blocks: List<PlannerPromptContextBlock>): LlmTextPart? {
        if (blocks.isEmpty()) {
            return null
        }
        return LlmTextPart(
                buildString {
                    append("[planner_context]\n")
                    blocks.forEachIndexed { index, block ->
                        if (index > 0) {
                            append('\n')
                        }
                        append('[')
                        append(block.name)
                        append("]\n")
                        append(block.content.trim())
                        append("\n[/")
                        append(block.name)
                        append("]\n")
                    }
                    append("[/planner_context]")
                }
        )
    }

    private fun contentPartsFrom(value: Any?, fallbackText: String): List<LlmContentPart> {
        val parts = mutableListOf<LlmContentPart>()
        @Suppress("UNCHECKED_CAST")
        val blocks = value as? List<Map<String, Any?>>
        blocks?.forEach { block ->
            when (block["type"]?.toString()) {
                "text" -> {
                    val text = block["text"]?.toString().orEmpty()
                    if (text.isNotBlank()) {
                        parts.add(LlmTextPart(text))
                    }
                }
                "image_url" -> {
                    imageUrlFrom(block["image_url"])?.let { parts.add(it) }
                }
            }
        }

        if (parts.isEmpty() && fallbackText.isNotBlank()) {
            parts.add(LlmTextPart(fallbackText))
        }
        return parts
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

    companion object {
        const val DEFAULT_SYSTEM_PROMPT: String =
                "你是「小千」这个角色背后的对话规划器。小千是一个外表文静、内心有点小傲娇的女大学生，说话简短、平淡，偶尔吐槽。请基于小千的人设直接回复用户，尽量使用用户的语言（默认中文），回复保持简短，除非用户明确要求详细说明。"
    }
}

data class PlannerPromptContextBlock(
        val name: String,
        val content: String
) {
    init {
        require(name.matches(Regex("[A-Za-z0-9_]+"))) {
            "Planner prompt context block name must be alphanumeric or underscore"
        }
        require(content.isNotBlank()) { "Planner prompt context block content must not be blank" }
    }
}

fun interface PlannerPromptContextProvider {
    suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock>
}

object EmptyPlannerPromptContextProvider : PlannerPromptContextProvider {
    override suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock> =
            emptyList()
}

/** Combine several providers' blocks in order. */
class CompositePlannerPromptContextProvider(
        private val providers: List<PlannerPromptContextProvider>
) : PlannerPromptContextProvider {
    override suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock> =
            providers.flatMap { it.blocksFor(context) }
}

fun interface PlannerSystemPromptProvider {
    suspend fun systemPromptFor(context: PlannerTurnContext): String?
}

object EmptyPlannerSystemPromptProvider : PlannerSystemPromptProvider {
    override suspend fun systemPromptFor(context: PlannerTurnContext): String? = null
}

private fun String?.trimmedOrNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }
