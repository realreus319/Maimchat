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
 * Instrumented coverage for [ChatDatabase.MIGRATION_2_3]: the recreate-table migration that drops
 * the remote-only `platform` and `receiver_user_id` columns (and the latter's index) from
 * `standard_messages` while preserving the surviving rows and columns.
 *
 * [MigrationTestHelper.runMigrationsAndValidate] validates the post-migration schema against the
 * exported 3.json (identityHash + columns + indices), which is the blind spot a hand-written
 * recreate-table migration is most likely to drift from.
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

    companion object {
        private const val TEST_DB = "chat-database-migration-test"
    }
}
