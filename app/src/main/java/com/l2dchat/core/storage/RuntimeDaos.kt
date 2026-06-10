package com.l2dchat.core.storage

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
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

    @Query(
            """
            SELECT * FROM planner_messages
            WHERE round_id = :roundId
            ORDER BY sequence ASC
            """
    )
    suspend fun queryPlannerMessages(roundId: String): List<PlannerMessageEntity>
}

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
}
