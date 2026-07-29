package com.l2dchat.core.reply

/**
 * Surfaces the currently active/queued DETACHED background workers (their goals + live status) into
 * EVERY planner turn, so the planner is always aware of what's running off-turn and can decide to
 * answer normally, wait, or act on a running task. Emits nothing when no workers are active.
 */
class BackgroundWorkerContextProvider(
    /** (contextId, agentId) → a human-readable status list, or null if none are active. */
    private val statusFor: (String, String) -> String?
) : PlannerPromptContextProvider {
    override suspend fun blocksFor(context: PlannerTurnContext): List<PlannerPromptContextBlock> {
        val text =
                statusFor(context.routingKey.contextId, context.routingKey.agentId)
                        ?: return emptyList()
        return listOf(PlannerPromptContextBlock(name = "active_background_tasks", content = text))
    }
}
