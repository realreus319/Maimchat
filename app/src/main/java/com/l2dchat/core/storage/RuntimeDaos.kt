package com.l2dchat.core.storage

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PlannerMessageEntity
import com.l2dchat.core.message.PlannerRoundEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.message.StandardMessageEntity
import com.l2dchat.core.message.ToolTaskEntity
import com.l2dchat.core.message.VisibleMessageEntity

@Dao
interface RuntimeMessageDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun appendMessage(message: VisibleMessageEntity)

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun appendStandardMessage(message: StandardMessageEntity)

    @Query(
            """
            DELETE FROM messages
            WHERE context_id = :contextId
              AND (:agentId IS NULL OR agent_id = :agentId)
            """
    )
    suspend fun deleteMessages(contextId: String, agentId: String?)

    @Query(
            """
            DELETE FROM standard_messages
            WHERE context_id = :contextId
              AND (:agentId IS NULL OR agent_id = :agentId)
            """
    )
    suspend fun deleteStandardMessages(contextId: String, agentId: String?)

    // Unscoped wipes for "清空聊天记录 = truly empty context". Conversation data is spread across
    // inconsistent context/agent ids (user id vs agent self-id), so a per-context delete misses rows.
    @Query("DELETE FROM messages") suspend fun deleteAllMessages()

    @Query("DELETE FROM standard_messages") suspend fun deleteAllStandardMessages()

    @Query(
            """
            SELECT * FROM messages
            WHERE context_id = :contextId
              AND (:agentId IS NULL OR agent_id = :agentId)
            ORDER BY timestamp_ms DESC
            LIMIT :limit
            """
    )
    suspend fun queryRecentMessages(
            contextId: String,
            agentId: String?,
            limit: Int
    ): List<VisibleMessageEntity>

    @Query(
            """
            SELECT * FROM standard_messages
            WHERE context_id = :contextId
              AND (:agentId IS NULL OR agent_id = :agentId)
            ORDER BY timestamp_ms DESC
            LIMIT :limit
            """
    )
    suspend fun queryRecentStandardMessages(
            contextId: String,
            agentId: String?,
            limit: Int
    ): List<StandardMessageEntity>

    @Query(
            """
            SELECT * FROM standard_messages
            WHERE context_id = :contextId
              AND (:agentId IS NULL OR agent_id = :agentId)
              AND timestamp_ms <= (
                  SELECT timestamp_ms FROM standard_messages WHERE message_id = :messageId
              )
            ORDER BY timestamp_ms DESC
            LIMIT :limit
            """
    )
    suspend fun queryStandardHistoryUntilMessage(
            contextId: String,
            agentId: String?,
            messageId: String,
            limit: Int
    ): List<StandardMessageEntity>
}

@Dao
interface PlannerStateDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun appendPlannerRound(round: PlannerRoundEntity)

    /** Trigger-message ids of orphan rounds (still 'generating', created before this process started). */
    @Query("SELECT trigger_message_id FROM planner_rounds WHERE state = 'generating' AND created_at_ms < :beforeMillis")
    suspend fun queryGeneratingRoundTriggerIds(beforeMillis: Long): List<String?>

    /**
     * Fail every round left in 'generating' that was created before [beforeMillis]. A round only
     * leaves 'generating' via the planner coroutine's finalizer, which a process SIGKILL skips — so on
     * a fresh process start any such row is an orphan that can never resume (in-flight coroutine/LLM/
     * worker handles are gone). The [beforeMillis] cutoff (= this process's start) ensures a round
     * created by the CURRENT process is never reaped out from under itself.
     */
    @Query(
            "UPDATE planner_rounds SET state = 'failed', updated_at_ms = :nowMillis " +
                    "WHERE state = 'generating' AND created_at_ms < :beforeMillis"
    )
    suspend fun failGeneratingRounds(nowMillis: Long, beforeMillis: Long): Int

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun appendPlannerMessage(message: PlannerMessageEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun appendToolTask(task: ToolTaskEntity)

    @Query(
            """
            UPDATE tool_tasks
            SET state = :state,
                output_json = :outputJson,
                error = :error,
                updated_at_ms = :updatedAtMillis
            WHERE task_id = :taskId
            """
    )
    suspend fun updateToolTaskState(
            taskId: String,
            state: String,
            outputJson: String?,
            error: String?,
            updatedAtMillis: Long
    )

    // Unscoped session-log wipes for "清空聊天记录" — runtime planner history (not part of the LLM
    // prompt, but tied to the cleared conversation; cleared for a true clean slate).
    @Query("DELETE FROM planner_rounds") suspend fun deleteAllPlannerRounds()

    @Query("DELETE FROM planner_messages") suspend fun deleteAllPlannerMessages()

    @Query("DELETE FROM tool_tasks") suspend fun deleteAllToolTasks()

    @Query(
            """
            SELECT * FROM planner_messages
            WHERE round_id = :roundId
            ORDER BY sequence ASC
            """
    )
    suspend fun queryPlannerMessages(roundId: String): List<PlannerMessageEntity>

    /**
     * Recent COMPLETED user-message rounds together with their FINAL assistant reply text. Used by the
     * reconcile/poll reply delivery: a reply is always durably saved here (planner_messages), but the
     * chat UI push can be lost to delivery timing (await timeout / process death) — the reconciler
     * re-delivers any reply that never reached the chat, deduped by a stable id. Only MSG-triggered
     * rounds (trigger_message_id LIKE 'msg%') — env/proactive rounds have their own delivery and must
     * NOT surface as chat turns.
     */
    @Query(
            """
            SELECT r.round_id AS roundId, r.trigger_message_id AS triggerMessageId,
                   m.content AS replyText, r.created_at_ms AS createdAtMs
            FROM planner_rounds r
            JOIN planner_messages m ON m.round_id = r.round_id AND m.role = 'assistant'
                AND m.sequence = (
                    SELECT MAX(sequence) FROM planner_messages
                    WHERE round_id = r.round_id AND role = 'assistant'
                )
            WHERE r.state = 'completed' AND r.trigger_message_id LIKE 'msg%'
                AND r.created_at_ms >= :sinceMs AND m.content IS NOT NULL AND m.content != ''
            ORDER BY r.created_at_ms ASC
            """
    )
    suspend fun queryRecentCompletedReplies(sinceMs: Long): List<CompletedReplyRow>
}

/** One completed user-message round + its final assistant reply text (reconcile/poll delivery). */
data class CompletedReplyRow(
        val roundId: String,
        val triggerMessageId: String?,
        val replyText: String,
        val createdAtMs: Long
)

@Dao
interface RuntimeStateDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAgentConfig(config: AgentConfigEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertPromptTemplate(template: PromptTemplateEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertMemory(memory: MemoryEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertImpression(impression: ImpressionEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertMoodState(moodState: MoodStateEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity)

    @Query(
            """
            SELECT * FROM media_blocks
            WHERE message_id = :messageId
            ORDER BY sequence ASC
            """
    )
    suspend fun queryMediaBlocksForMessage(messageId: String): List<MediaBlockEntity>

    @Query("SELECT * FROM agent_configs WHERE agent_id = :agentId LIMIT 1")
    suspend fun queryAgentConfig(agentId: String): AgentConfigEntity?

    @Query(
            """
            SELECT * FROM prompt_templates
            WHERE name = :name
              AND (agent_id = :agentId OR agent_id IS NULL)
            ORDER BY CASE WHEN agent_id = :agentId THEN 0 ELSE 1 END,
                     updated_at_ms DESC
            LIMIT 1
            """
    )
    suspend fun queryPromptTemplate(name: String, agentId: String): PromptTemplateEntity?

    @Query("SELECT * FROM prompt_templates WHERE template_id = :templateId LIMIT 1")
    suspend fun queryPromptTemplateById(templateId: String): PromptTemplateEntity?

    @Query(
            """
            SELECT * FROM memories
            WHERE context_id = :contextId
              AND agent_id = :agentId
            ORDER BY importance DESC, updated_at_ms DESC
            LIMIT :limit
            """
    )
    suspend fun queryMemories(contextId: String, agentId: String, limit: Int): List<MemoryEntity>

    @Query(
            """
            SELECT COUNT(*) FROM memories
            WHERE context_id = :contextId AND agent_id = :agentId
            """
    )
    suspend fun countMemories(contextId: String, agentId: String): Int

    @Query(
            """
            SELECT * FROM memories
            WHERE context_id = :contextId
              AND agent_id = :agentId
              AND content = :content
            LIMIT 1
            """
    )
    suspend fun queryMemoryByContent(
            contextId: String,
            agentId: String,
            content: String
    ): MemoryEntity?

    @Query(
            """
            UPDATE memories
            SET access_count = :accessCount,
                last_access_ms = :lastAccessMillis
            WHERE memory_id = :memoryId
            """
    )
    suspend fun reinforceMemory(memoryId: String, accessCount: Int, lastAccessMillis: Long)

    @Query("DELETE FROM memories WHERE memory_id = :memoryId")
    suspend fun deleteMemory(memoryId: String)

    // Unscoped persona-state wipes for "清空聊天记录" — these (memories/impressions/mood) are
    // RE-INJECTED into the planner+replier prompts every turn, so they MUST be cleared for the context
    // to actually be empty; the durable memory_store, the user impression/relationship summary, and
    // the carried-over mood would otherwise survive a chat clear.
    @Query("DELETE FROM memories") suspend fun deleteAllMemories()

    @Query("DELETE FROM impressions") suspend fun deleteAllImpressions()

    @Query("DELETE FROM mood_state") suspend fun deleteAllMoodState()

    @Query("DELETE FROM media_blocks") suspend fun deleteAllMediaBlocks()

    @Query(
            """
            SELECT * FROM impressions
            WHERE context_id = :contextId
              AND agent_id = :agentId
              AND subject_id = :subjectId
            LIMIT 1
            """
    )
    suspend fun queryImpression(
            contextId: String,
            agentId: String,
            subjectId: String
    ): ImpressionEntity?

    @Query(
            """
            SELECT * FROM impressions
            WHERE context_id = :contextId
              AND agent_id = :agentId
            ORDER BY updated_at_ms DESC
            LIMIT :limit
            """
    )
    suspend fun queryImpressions(
            contextId: String,
            agentId: String,
            limit: Int
    ): List<ImpressionEntity>

    @Query(
            """
            SELECT * FROM mood_state
            WHERE context_id = :contextId
              AND agent_id = :agentId
            LIMIT 1
            """
    )
    suspend fun queryMoodState(contextId: String, agentId: String): MoodStateEntity?

    /** Reactive mood for the mood→Live2D-motion mapping: emits on every upsert (LLM mood update). */
    @Query(
            """
            SELECT * FROM mood_state
            WHERE context_id = :contextId
              AND agent_id = :agentId
            LIMIT 1
            """
    )
    fun observeMoodState(contextId: String, agentId: String): Flow<MoodStateEntity?>
}
