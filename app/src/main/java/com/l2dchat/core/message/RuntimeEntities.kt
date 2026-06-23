package com.l2dchat.core.message

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
        tableName = "messages",
        indices = [Index(value = ["context_id", "agent_id", "timestamp_ms"])]
)
data class VisibleMessageEntity(
        @PrimaryKey @ColumnInfo(name = "message_id") val messageId: String,
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "agent_id") val agentId: String?,
        @ColumnInfo(name = "content") val content: String,
        @ColumnInfo(name = "is_from_user") val isFromUser: Boolean,
        @ColumnInfo(name = "timestamp_ms") val timestampMillis: Long,
        @ColumnInfo(name = "motion_group") val motionGroup: String? = null,
        @ColumnInfo(name = "motion_index") val motionIndex: Int? = null,
        @ColumnInfo(name = "motion_loop") val motionLoop: Boolean = false
)

@Entity(
        tableName = "standard_messages",
        indices =
                [
                        Index(value = ["context_id", "agent_id", "timestamp_ms"]),
                        Index(value = ["sender_user_id"]),
                        Index(value = ["receiver_user_id"])
                ]
)
data class StandardMessageEntity(
        @PrimaryKey @ColumnInfo(name = "message_id") val messageId: String,
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "agent_id") val agentId: String?,
        @ColumnInfo(name = "platform") val platform: String?,
        @ColumnInfo(name = "sender_user_id") val senderUserId: String?,
        @ColumnInfo(name = "receiver_user_id") val receiverUserId: String?,
        @ColumnInfo(name = "timestamp_ms") val timestampMillis: Long,
        @ColumnInfo(name = "raw_text") val rawText: String?,
        @ColumnInfo(name = "message_json") val messageJson: String
)

@Entity(
        tableName = "planner_rounds",
        indices = [Index(value = ["context_id", "agent_id", "created_at_ms"])]
)
data class PlannerRoundEntity(
        @PrimaryKey @ColumnInfo(name = "round_id") val roundId: String,
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "agent_id") val agentId: String,
        @ColumnInfo(name = "trigger_message_id") val triggerMessageId: String?,
        @ColumnInfo(name = "state") val state: String,
        @ColumnInfo(name = "created_at_ms") val createdAtMillis: Long,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(
        tableName = "planner_messages",
        indices = [Index(value = ["round_id", "sequence"])]
)
data class PlannerMessageEntity(
        @PrimaryKey @ColumnInfo(name = "planner_message_id") val plannerMessageId: String,
        @ColumnInfo(name = "round_id") val roundId: String,
        @ColumnInfo(name = "sequence") val sequence: Int,
        @ColumnInfo(name = "role") val role: String,
        @ColumnInfo(name = "content") val content: String?,
        @ColumnInfo(name = "tool_call_id") val toolCallId: String?,
        @ColumnInfo(name = "payload_json") val payloadJson: String?,
        @ColumnInfo(name = "created_at_ms") val createdAtMillis: Long
)

@Entity(tableName = "agent_configs")
data class AgentConfigEntity(
        @PrimaryKey @ColumnInfo(name = "agent_id") val agentId: String,
        @ColumnInfo(name = "display_name") val displayName: String,
        @ColumnInfo(name = "persona") val persona: String?,
        @ColumnInfo(name = "provider") val provider: String?,
        @ColumnInfo(name = "model") val model: String?,
        @ColumnInfo(name = "settings_json") val settingsJson: String?,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(
        tableName = "prompt_templates",
        indices = [Index(value = ["agent_id", "name"])]
)
data class PromptTemplateEntity(
        @PrimaryKey @ColumnInfo(name = "template_id") val templateId: String,
        @ColumnInfo(name = "agent_id") val agentId: String?,
        @ColumnInfo(name = "name") val name: String,
        @ColumnInfo(name = "body") val body: String,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(
        tableName = "tool_tasks",
        indices = [Index(value = ["round_id", "state"]), Index(value = ["tool_name"])]
)
data class ToolTaskEntity(
        @PrimaryKey @ColumnInfo(name = "task_id") val taskId: String,
        @ColumnInfo(name = "round_id") val roundId: String,
        @ColumnInfo(name = "tool_name") val toolName: String,
        @ColumnInfo(name = "state") val state: String,
        @ColumnInfo(name = "input_json") val inputJson: String?,
        @ColumnInfo(name = "output_json") val outputJson: String?,
        @ColumnInfo(name = "error") val error: String?,
        @ColumnInfo(name = "created_at_ms") val createdAtMillis: Long,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(
        tableName = "memories",
        indices =
                [
                        Index(value = ["context_id", "agent_id", "updated_at_ms"]),
                        Index(value = ["context_id", "agent_id", "importance"])
                ]
)
data class MemoryEntity(
        @PrimaryKey @ColumnInfo(name = "memory_id") val memoryId: String,
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "agent_id") val agentId: String,
        @ColumnInfo(name = "content") val content: String,
        @ColumnInfo(name = "importance") val importance: Double,
        @ColumnInfo(name = "category") val category: String = "general",
        @ColumnInfo(name = "access_count") val accessCount: Int = 0,
        @ColumnInfo(name = "last_access_ms") val lastAccessMillis: Long = 0,
        @ColumnInfo(name = "created_at_ms") val createdAtMillis: Long,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(
        tableName = "impressions",
        indices = [Index(value = ["context_id", "agent_id", "subject_id"], unique = true)]
)
data class ImpressionEntity(
        @PrimaryKey @ColumnInfo(name = "impression_id") val impressionId: String,
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "agent_id") val agentId: String,
        @ColumnInfo(name = "subject_id") val subjectId: String,
        @ColumnInfo(name = "content") val content: String,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(tableName = "mood_state", primaryKeys = ["context_id", "agent_id"])
data class MoodStateEntity(
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "agent_id") val agentId: String,
        @ColumnInfo(name = "valence") val valence: Double,
        @ColumnInfo(name = "arousal") val arousal: Double,
        @ColumnInfo(name = "state_json") val stateJson: String?,
        @ColumnInfo(name = "updated_at_ms") val updatedAtMillis: Long
)

@Entity(
        tableName = "media_blocks",
        indices =
                [
                        Index(value = ["message_id", "sequence"]),
                        Index(value = ["context_id", "created_at_ms"])
                ]
)
data class MediaBlockEntity(
        @PrimaryKey @ColumnInfo(name = "media_id") val mediaId: String,
        @ColumnInfo(name = "message_id") val messageId: String?,
        @ColumnInfo(name = "context_id") val contextId: String,
        @ColumnInfo(name = "sequence") val sequence: Int,
        @ColumnInfo(name = "type") val type: String,
        @ColumnInfo(name = "uri") val uri: String,
        @ColumnInfo(name = "mime_type") val mimeType: String?,
        @ColumnInfo(name = "metadata_json") val metadataJson: String?,
        @ColumnInfo(name = "created_at_ms") val createdAtMillis: Long
)
