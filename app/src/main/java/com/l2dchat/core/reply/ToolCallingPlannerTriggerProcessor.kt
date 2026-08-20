package com.l2dchat.core.reply

import com.google.gson.Gson
import com.google.gson.JsonParser
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.llm.LlmToolResult
import com.l2dchat.core.mem.Multimodal
import com.l2dchat.core.tools.ReplierTool
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.tools.ToolRegistry
import com.l2dchat.core.trigger.TriggerType

class ToolCallingPlannerTriggerProcessor(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val toolRegistry: ToolRegistry,
        private val promptBuilder: PlannerPromptBuilder = PlannerPromptBuilder(),
        private val systemPromptProvider: PlannerSystemPromptProvider =
                EmptyPlannerSystemPromptProvider,
        private val toolMode: ToolExecutionMode = ToolExecutionMode.NORMAL,
        private val mediaReader: LtmMediaReader = LtmMediaReader.DEFAULT,
) : PlannerTriggerProcessor {
    init {
        require(toolRegistry.definitionsFor(toolMode).isNotEmpty()) {
            "ToolCallingPlannerTriggerProcessor requires at least one ${toolMode.name.lowercase()} tool"
        }
    }

    private val timingLogger =
            com.l2dchat.logging.L2DLogger.module(com.l2dchat.logging.LogModule.CHAT)

    override suspend fun process(context: PlannerTurnContext) {
        val turnStart = System.currentTimeMillis()
        timingLogger.info("[timing] === turn start (round=${context.roundId}) ===")
        var replySent = false
        val toolDefinitions = toolRegistry.definitionsFor(toolMode)
        val toolContext =
                ToolExecutionContext(
                        loopId = context.loopId,
                        routingKey = context.routingKey,
                        trigger = context.trigger,
                        foregroundEpoch = context.foregroundEpoch,
                        roundId = context.roundId,
                        mode = toolMode
                )
        llmClient.chatCompletionWithTools(
                messages =
                        promptBuilder.buildMessages(
                                context = context,
                                systemPromptOverride = systemPromptProvider.systemPromptFor(context)
                        ),
                tools = toolDefinitions,
                config = config,
                toolExecutor =
                        LlmToolExecutor { toolCall ->
                            val execution = toolRegistry.execute(toolContext, toolCall)
                            if (!execution.result.isError && execution.result.replyText != null) {
                                val sendResult = context.sendReply(execution.result.replyText)
                                replySent = replySent || sendResult.sent
                            }
                            execution.toLlmToolResult().withMediaInjection(toolCall, mediaReader)
                        }
        )

        if (!replySent && context.trigger.triggerType == TriggerType.SYS) {
            // A worker-completion (SYS) trigger MUST deliver its result. The planner LLM occasionally
            // returns STOP without calling the replier, which would SILENTLY DROP the worker's answer
            // (the user asked for it and never hears back). SYS delivery is not a place to surface a
            // defect by dropping output — force one replier call so the completion always lands. The
            // completion text lives in the trigger payload and is passed as the replier's `thinking`.
            val forced = forceReplier(toolContext, context)
            if (forced.isNotBlank()) {
                val sendResult = context.sendReply(forced)
                replySent = replySent || sendResult.sent
                timingLogger.info(
                        "[timing] SYS completion had no reply — forced replier (sent=${sendResult.sent})"
                )
            }
        }
        if (!replySent) {
            // A non-SYS turn ended WITHOUT a reply. This stays surfaced (not force-replied) so a real
            // planner defect (wrong-mode routing, early stop, missing tool) is not masked over.
            timingLogger.info(
                    "[timing] !! planner ended WITHOUT sending a reply after " +
                            "${System.currentTimeMillis() - turnStart}ms (mode=${toolMode.name}) — " +
                            "no reply sent (forceReplier kept only for SYS completions)"
            )
        }
        timingLogger.info(
                "[timing] === turn done in ${System.currentTimeMillis() - turnStart}ms " +
                        "(replySent=$replySent) ==="
        )
    }

    /**
     * Force one [ReplierTool] call (a couple of retries) and return its composed reply, or "" on
     * persistent failure. Used only to guarantee SYS worker-completion delivery. The completion text
     * (trigger payload) is passed as the replier's `thinking` so it has the result to voice.
     */
    private suspend fun forceReplier(
            toolContext: ToolExecutionContext,
            context: PlannerTurnContext
    ): String {
        val completionText = (context.trigger.payload["text"] as? String)?.takeIf { it.isNotBlank() }
        val thinking =
                completionText
                        ?: "请根据后台任务的完成结果，用角色口吻把结果整理后回复给用户一次。"
        val argumentsJson =
                com.google.gson.JsonObject().apply { addProperty("thinking", thinking) }.toString()
        repeat(REPLIER_FORCE_ATTEMPTS) { attempt ->
            val call =
                    LlmToolCall(
                            id = "forced-sys-replier-${context.roundId}-$attempt",
                            name = ReplierTool.NAME,
                            argumentsJson = argumentsJson
                    )
            val execution = runCatching { toolRegistry.execute(toolContext, call) }.getOrNull()
            val text = execution?.result?.replyText?.trim()?.ifBlank { null }
            if (execution != null && !execution.result.isError && text != null) {
                return text
            }
        }
        return ""
    }

    companion object {
        private const val REPLIER_FORCE_ATTEMPTS = 2
    }
}

/**
 * Reads a media file's bytes as a base64 data URL for VLM injection (T5).
 *
 * The default implementation delegates to [Multimodal.dataUrl], which enforces the
 * [Multimodal.MAX_IMAGE_BYTES] guard and infers the mime from the extension. Tests
 * inject a fake to avoid touching the filesystem.
 */
fun interface LtmMediaReader {
    fun readDataUrl(mediaPath: String): String

    companion object {
        val DEFAULT: LtmMediaReader = LtmMediaReader { path -> Multimodal.dataUrl(path) }
    }
}

/**
 * T5 host-side post-processing: when this is an `ltm_read_media` result for a photo (modality ==
 * "photo", no error, media_path present), read the file's bytes and attach them as an
 * `image_url` content part so the VLM sees the picture on its next turn. Video/voice results and
 * error results pass through unchanged (a VLM cannot consume video/voice as an image).
 *
 * The payload shape (`media_path`/`modality`/`captured_at`/`error`) is NOT modified — this is a
 * purely additive, host-side injection layered on top of the Python-faithful tool result.
 */
private fun LlmToolResult.withMediaInjection(
        toolCall: LlmToolCall,
        mediaReader: LtmMediaReader,
): LlmToolResult {
    if (isError || name != LTM_READ_MEDIA_TOOL) return this
    val parsed = runCatching { JsonParser.parseString(content).asJsonObject }.getOrNull() ?: return this
    if (parsed.get("error")?.takeIf { !it.isJsonNull } != null) return this
    val modality = parsed.get("modality")?.takeIf { !it.isJsonNull }?.asString
    if (modality != "photo") return this
    val mediaPath = parsed.get("media_path")?.takeIf { !it.isJsonNull }?.asString
    if (mediaPath.isNullOrBlank()) return this
    val dataUrl = runCatching { mediaReader.readDataUrl(mediaPath) }.getOrNull() ?: return this
    val parts: List<LlmContentPart> = listOf(
            LlmTextPart("[media injected: $mediaPath]"),
            LlmImageUrlPart(url = dataUrl),
    )
    return copy(contentParts = parts)
}

private const val LTM_READ_MEDIA_TOOL: String = "ltm_read_media"
