package com.l2dchat.core.storage

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
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

@Database(
        entities =
                [
                        VisibleMessageEntity::class,
                        StandardMessageEntity::class,
                        PlannerRoundEntity::class,
                        PlannerMessageEntity::class,
                        AgentConfigEntity::class,
                        PromptTemplateEntity::class,
                        ToolTaskEntity::class,
                        MemoryEntity::class,
                        ImpressionEntity::class,
                        MoodStateEntity::class,
                        MediaBlockEntity::class
                ],
        version = 4,
        exportSchema = true
)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun runtimeMessageDao(): RuntimeMessageDao

    abstract fun plannerStateDao(): PlannerStateDao

    abstract fun runtimeStateDao(): RuntimeStateDao

    companion object {
        private const val DATABASE_NAME = "maimchat_runtime.db"

        @Volatile private var instance: ChatDatabase? = null

        /**
         * v1 -> v2: long-term memory upgrade. Adds memory category, access-count and
         * last-access reinforcement columns plus an importance index for scoring/eviction.
         * Data-preserving (no destructive fallback).
         */
        val MIGRATION_1_2: Migration =
                object : Migration(1, 2) {
                    override fun migrate(db: SupportSQLiteDatabase) {
                        db.execSQL(
                                "ALTER TABLE memories ADD COLUMN category TEXT NOT NULL DEFAULT 'general'"
                        )
                        db.execSQL(
                                "ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"
                        )
                        db.execSQL(
                                "ALTER TABLE memories ADD COLUMN last_access_ms INTEGER NOT NULL DEFAULT 0"
                        )
                        db.execSQL(
                                "CREATE INDEX IF NOT EXISTS index_memories_context_id_agent_id_importance " +
                                        "ON memories (context_id, agent_id, importance)"
                        )
                    }
                }

        /**
         * v2 -> v3: pure-LOCAL cleanup. Drops the remote-only `platform` and the
         * `receiver_user_id` columns (and the latter's index) from `standard_messages`.
         * SQLite cannot drop columns in place on old engines, so recreate the table with the
         * trimmed schema and copy the preserved rows over. Data-preserving (no destructive fallback).
         */
        val MIGRATION_2_3: Migration =
                object : Migration(2, 3) {
                    override fun migrate(db: SupportSQLiteDatabase) {
                        db.execSQL(
                                "CREATE TABLE IF NOT EXISTS `standard_messages_new` (" +
                                        "`message_id` TEXT NOT NULL, " +
                                        "`context_id` TEXT NOT NULL, " +
                                        "`agent_id` TEXT, " +
                                        "`sender_user_id` TEXT, " +
                                        "`timestamp_ms` INTEGER NOT NULL, " +
                                        "`raw_text` TEXT, " +
                                        "`message_json` TEXT NOT NULL, " +
                                        "PRIMARY KEY(`message_id`))"
                        )
                        db.execSQL(
                                "INSERT INTO `standard_messages_new` (" +
                                        "`message_id`, `context_id`, `agent_id`, `sender_user_id`, " +
                                        "`timestamp_ms`, `raw_text`, `message_json`) " +
                                        "SELECT `message_id`, `context_id`, `agent_id`, `sender_user_id`, " +
                                        "`timestamp_ms`, `raw_text`, `message_json` FROM `standard_messages`"
                        )
                        db.execSQL("DROP TABLE `standard_messages`")
                        db.execSQL(
                                "ALTER TABLE `standard_messages_new` RENAME TO `standard_messages`"
                        )
                        db.execSQL(
                                "CREATE INDEX IF NOT EXISTS " +
                                        "`index_standard_messages_context_id_agent_id_timestamp_ms` " +
                                        "ON `standard_messages` (`context_id`, `agent_id`, `timestamp_ms`)"
                        )
                        db.execSQL(
                                "CREATE INDEX IF NOT EXISTS " +
                                        "`index_standard_messages_sender_user_id` " +
                                        "ON `standard_messages` (`sender_user_id`)"
                        )
                    }
                }

        /**
         * v3 -> v4: persist the inline AI-agent activity bubble and worker-submitted file bubble into
         * the visible `messages` table so they survive an app restart / history reload like normal
         * chat messages. Two nullable TEXT columns. Data-preserving (no destructive fallback).
         */
        val MIGRATION_3_4: Migration =
                object : Migration(3, 4) {
                    override fun migrate(db: SupportSQLiteDatabase) {
                        db.execSQL("ALTER TABLE messages ADD COLUMN agent_activity_json TEXT")
                        db.execSQL("ALTER TABLE messages ADD COLUMN file_info_json TEXT")
                    }
                }

        fun getInstance(context: Context): ChatDatabase =
                instance
                        ?: synchronized(this) {
                            instance
                                    ?: Room.databaseBuilder(
                                                    context.applicationContext,
                                                    ChatDatabase::class.java,
                                                    DATABASE_NAME
                                            )
                                            .addMigrations(
                                                    MIGRATION_1_2,
                                                    MIGRATION_2_3,
                                                    MIGRATION_3_4
                                            )
                                            .build()
                                            .also { instance = it }
                        }
    }
}
