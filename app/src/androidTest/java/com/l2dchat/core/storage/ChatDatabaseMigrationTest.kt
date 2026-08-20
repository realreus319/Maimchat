package com.l2dchat.core.storage

import androidx.room.testing.MigrationTestHelper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Instrumented coverage for [ChatDatabase]'s hand-written migrations. These tests create real
 * databases at the old versions, execute the production migration objects, and ask Room to
 * validate the resulting schema against the exported JSON.
 *
 * [MigrationTestHelper.runMigrationsAndValidate] validates each post-migration schema against its
 * exported JSON (identity hash + columns + indices), which is the blind spot hand-written DDL is
 * most likely to drift from.
 *
 * NOTE: This is an instrumented test and needs a connected device/emulator. Run via
 *   ./gradlew :app:connectedDebugAndroidTest
 * (optionally with a class filter, e.g.
 *   -Pandroid.testInstrumentationRunnerArguments.class=com.l2dchat.core.storage.ChatDatabaseMigrationTest)
 */
@RunWith(AndroidJUnit4::class)
class ChatDatabaseMigrationTest {

    @get:Rule
    val helper: MigrationTestHelper =
            MigrationTestHelper(
                    InstrumentationRegistry.getInstrumentation(),
                    ChatDatabase::class.java
            )

    @Test
    fun migrate2To3_dropsRemoteColumns_andPreservesRows() {
        // Seed a v2 database with a standard_messages row that carries the to-be-dropped
        // `platform` and `receiver_user_id` values, plus the columns the migration must keep.
        helper.createDatabase(TEST_DB, 2).use { db ->
            db.execSQL(
                    "INSERT INTO standard_messages (" +
                            "message_id, context_id, agent_id, platform, sender_user_id, " +
                            "receiver_user_id, timestamp_ms, raw_text, message_json) " +
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    arrayOf<Any?>(
                            "msg-1",
                            "room-a",
                            "hiyori",
                            "qq",
                            "alice",
                            "hiyori",
                            1234L,
                            "hello",
                            "{\"k\":\"v\"}"
                    )
            )
        }

        // Run the real migration and validate the resulting schema against 3.json.
        val db = helper.runMigrationsAndValidate(TEST_DB, 3, true, ChatDatabase.MIGRATION_2_3)

        // (i) The row survives and the preserved columns keep their values.
        db.query(
                        "SELECT message_id, context_id, agent_id, sender_user_id, " +
                                "timestamp_ms, raw_text, message_json FROM standard_messages"
                )
                .use { cursor ->
                    assertTrue("migrated row must still be present", cursor.moveToFirst())
                    assertEquals(1, cursor.count)
                    assertEquals("msg-1", cursor.getString(0))
                    assertEquals("room-a", cursor.getString(1))
                    assertEquals("hiyori", cursor.getString(2))
                    assertEquals("alice", cursor.getString(3))
                    assertEquals(1234L, cursor.getLong(4))
                    assertEquals("hello", cursor.getString(5))
                    assertEquals("{\"k\":\"v\"}", cursor.getString(6))
                }

        // (ii) The dropped columns are gone from the table definition.
        val columns = mutableSetOf<String>()
        db.query("PRAGMA table_info(standard_messages)").use { cursor ->
            val nameIndex = cursor.getColumnIndexOrThrow("name")
            while (cursor.moveToNext()) {
                columns.add(cursor.getString(nameIndex))
            }
        }
        assertFalse("platform column must be dropped", columns.contains("platform"))
        assertFalse("receiver_user_id column must be dropped", columns.contains("receiver_user_id"))
        assertTrue(columns.contains("message_id"))
        assertTrue(columns.contains("sender_user_id"))
    }

    @Test
    fun migrate4To5_dropsLegacyMemories_andCreatesMemGraphTables() {
        helper.createDatabase(MEM_TEST_DB, 4).use { db ->
            db.execSQL(
                    "INSERT INTO memories (" +
                            "memory_id, context_id, agent_id, content, importance, category, " +
                            "access_count, last_access_ms, created_at_ms, updated_at_ms) " +
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    arrayOf<Any?>(
                            "legacy-memory",
                            "room-a",
                            "hiyori",
                            "legacy content",
                            0.8,
                            "general",
                            2,
                            1234L,
                            1000L,
                            1200L
                    )
            )
        }

        val db =
                helper.runMigrationsAndValidate(
                        MEM_TEST_DB,
                        5,
                        true,
                        ChatDatabase.MIGRATION_4_5
                )

        val tables = mutableSetOf<String>()
        db.query("SELECT name FROM sqlite_master WHERE type = 'table'").use { cursor ->
            while (cursor.moveToNext()) tables.add(cursor.getString(0))
        }

        assertFalse("legacy memories table must be dropped", tables.contains("memories"))
        listOf("mem_nodes", "mem_event_entity_links", "mem_vectors", "mem_receipts").forEach {
            assertTrue("$it must exist after migration", tables.contains(it))
        }
    }

    companion object {
        private const val TEST_DB = "chat-database-migration-test"
        private const val MEM_TEST_DB = "chat-database-mem-migration-test"
    }
}
