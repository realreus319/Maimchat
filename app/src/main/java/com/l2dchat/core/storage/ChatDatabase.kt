package com.l2dchat.core.storage

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import com.l2dchat.core.mem.MemEventEntityLinkEntity
import com.l2dchat.core.mem.MemLinkDao
import com.l2dchat.core.mem.MemNodeDao
import com.l2dchat.core.mem.MemNodeEntity
import com.l2dchat.core.mem.MemReceiptDao
import com.l2dchat.core.mem.MemReceiptEntity
import com.l2dchat.core.mem.MemVectorDao
import com.l2dchat.core.mem.MemVectorEntity
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
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
                        ImpressionEntity::class,
                        MoodStateEntity::class,
                        MediaBlockEntity::class,
                        MemNodeEntity::class,
                        MemEventEntityLinkEntity::class,
                        MemVectorEntity::class,
                        MemReceiptEntity::class
                ],
        version = 5,
        exportSchema = true
)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun runtimeMessageDao(): RuntimeMessageDao

    abstract fun plannerStateDao(): PlannerStateDao

    abstract fun runtimeStateDao(): RuntimeStateDao

    abstract fun memNodeDao(): MemNodeDao

    abstract fun memLinkDao(): MemLinkDao

    abstract fun memVectorDao(): MemVectorDao

    abstract fun memReceiptDao(): MemReceiptDao

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

        /**
         * v4 -> v5: destructive replacement of the legacy long-term-memory subsystem.
         *
         * The old single-table `memories` store (keyword search + importance/recency eviction,
         * surfaced via the now-removed legacy memory tools) is dropped and replaced by the new
         * mem four-table schema (`mem_nodes` / `mem_event_entity_links` / `mem_vectors` /
         * `mem_receipts`) backing the content-addressed node graph + vector + BM25 memory engine.
         *
         * This is a locked destructive migration: existing users' legacy memory rows are NOT
         * preserved — the new schema is incompatible with the old one, and the old data has no
         * place in the node-graph model. The four new tables are created here with the exact
         * column types/indices/PKs declared by the `@Entity` annotations in `MemEntities.kt`,
         * so Room's runtime schema hash matches the KSP-generated one.
         */
        val MIGRATION_4_5: Migration =
                object : Migration(4, 5) {
                    override fun migrate(db: SupportSQLiteDatabase) {
                        // Drop the legacy single-table memory store. All its rows are discarded.
                        db.execSQL("DROP TABLE IF EXISTS memories")

                        // mem_nodes — content-addressed node store (events / entities / life_capture).
                        db.execSQL(
                                """
                                CREATE TABLE IF NOT EXISTS mem_nodes (
                                    hash_id TEXT NOT NULL PRIMARY KEY,
                                    base_type TEXT NOT NULL,
                                    node_type TEXT NOT NULL,
                                    content TEXT NOT NULL,
                                    mention_time INTEGER NOT NULL,
                                    event_time_raw TEXT,
                                    event_time TEXT,
                                    start_ts INTEGER,
                                    end_ts INTEGER,
                                    importance REAL,
                                    sentiment REAL,
                                    metadata_json TEXT NOT NULL,
                                    superseded_by TEXT,
                                    access_count INTEGER NOT NULL,
                                    last_accessed INTEGER,
                                    created_at INTEGER NOT NULL,
                                    updated_at INTEGER NOT NULL
                                )
                                """.trimIndent()
                        )
                        db.execSQL("CREATE INDEX IF NOT EXISTS index_mem_nodes_node_type ON mem_nodes(node_type)")
                        db.execSQL("CREATE INDEX IF NOT EXISTS index_mem_nodes_start_ts ON mem_nodes(start_ts)")
                        db.execSQL("CREATE INDEX IF NOT EXISTS index_mem_nodes_mention_time ON mem_nodes(mention_time)")

                        // mem_event_entity_links — directed event→entity edges with a role label.
                        db.execSQL(
                                """
                                CREATE TABLE IF NOT EXISTS mem_event_entity_links (
                                    event_hash TEXT NOT NULL,
                                    entity_hash TEXT NOT NULL,
                                    role TEXT NOT NULL,
                                    mention_count INTEGER NOT NULL,
                                    created_at INTEGER NOT NULL,
                                    PRIMARY KEY(event_hash, entity_hash, role)
                                )
                                """.trimIndent()
                        )
                        db.execSQL("CREATE INDEX IF NOT EXISTS index_mem_event_entity_links_entity_hash ON mem_event_entity_links(entity_hash)")

                        // mem_vectors — pre-computed embedding blobs (little-endian IEEE 754 floats).
                        db.execSQL(
                                """
                                CREATE TABLE IF NOT EXISTS mem_vectors (
                                    hash_id TEXT NOT NULL PRIMARY KEY,
                                    dim INTEGER NOT NULL,
                                    embedding BLOB NOT NULL
                                )
                                """.trimIndent()
                        )

                        // mem_receipts — idempotency receipts for ingest batches.
                        db.execSQL(
                                """
                                CREATE TABLE IF NOT EXISTS mem_receipts (
                                    batch_id TEXT NOT NULL PRIMARY KEY,
                                    source TEXT,
                                    status TEXT NOT NULL,
                                    node_hashes_json TEXT NOT NULL,
                                    link_count INTEGER NOT NULL,
                                    error TEXT,
                                    created_at INTEGER NOT NULL
                                )
                                """.trimIndent()
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
                                            .addMigrations(
                                                    MIGRATION_1_2,
                                                    MIGRATION_2_3,
                                                    MIGRATION_3_4,
                                                    MIGRATION_4_5
                                            )
                                            .build()
                                            .also { instance = it }
                        }
    }
}
