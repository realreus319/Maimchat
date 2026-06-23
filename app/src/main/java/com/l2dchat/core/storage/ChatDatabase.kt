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
        version = 2,
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

        fun getInstance(context: Context): ChatDatabase =
                instance
                        ?: synchronized(this) {
                            instance
                                    ?: Room.databaseBuilder(
                                                    context.applicationContext,
                                                    ChatDatabase::class.java,
                                                    DATABASE_NAME
                                            )
                                            .addMigrations(MIGRATION_1_2)
                                            .build()
                                            .also { instance = it }
                        }
    }
}
