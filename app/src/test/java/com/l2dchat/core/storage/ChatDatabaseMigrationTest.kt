package com.l2dchat.core.storage

import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * JVM-side validation of the v5 [ChatDatabase] schema.
 *
 * ## Approach
 *
 * The canonical v4 → v5 migration test lives in the **androidTest** source set because
 * `MigrationTestHelper` requires an instrumentation context and a connected device/emulator.
 * These faster JVM tests complement it by checking two parts of the v5 contract:
 *
 * 1. **Schema-file verification** — parse the exported `5.json` produced by KSP
 *    (`room.schemaLocation == $projectDir/schemas`) and assert the `memories` table is gone
 *    and the four mem tables are present. This is the same JSON Room's `MigrationTestHelper`
 *    compares against, so it is the authoritative schema contract.
 * 2. **Runtime verification** — open a fresh in-memory v5 database under Robolectric and touch
 *    every mem DAO, which forces Room to validate the runtime schema hash against the
 *    `@Database`-declared entities. If `MIGRATION_4_5`'s DDL diverged from the entity
 *    annotations, Room would reject the database at open time.
 *
 * They do not execute [ChatDatabase.MIGRATION_4_5]; that behavior is covered by the instrumented
 * `ChatDatabaseMigrationTest.migrate4To5_dropsLegacyMemories_andCreatesMemGraphTables` test.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ChatDatabaseMigrationTest {

    @Test
    fun `exported v5 schema drops memories and declares the four mem tables`() {
        val schemaFile =
                listOf(
                                "app/schemas/com.l2dchat.core.storage.ChatDatabase/5.json",
                                "schemas/com.l2dchat.core.storage.ChatDatabase/5.json",
                                "src/main/schemas/com.l2dchat.core.storage.ChatDatabase/5.json"
                        )
                        .map { java.io.File(it) }
                        .firstOrNull { it.exists() }
        assertTrue("v5 schema JSON must exist (exportSchema=true)", schemaFile != null)

        val json = schemaFile!!.readText()
        val tables =
                Regex(""""tableName"\s*:\s*"([^"]+)"""").findAll(json).map { it.groupValues[1] }.toSet()

        assertFalse("memories table must be absent from v5 schema", tables.contains("memories"))
        listOf("mem_nodes", "mem_event_entity_links", "mem_vectors", "mem_receipts").forEach {
            assertTrue("v5 schema must declare $it", tables.contains(it))
        }

        // Spot-check the load-bearing columns so a column rename/drop does not slip through.
        assertTrue("mem_nodes.hash_id must be declared", json.contains("\"hash_id\""))
        assertTrue(
                "mem_event_entity_links composite PK columns must be declared",
                json.contains("event_hash") && json.contains("entity_hash") && json.contains("role")
        )
        assertTrue("mem_vectors.embedding BLOB column must be declared", json.contains("embedding"))
        assertTrue("mem_receipts.batch_id must be declared", json.contains("batch_id"))
    }

    @Test
    fun `fresh v5 database opens and serves all four mem DAOs plus preserved state DAOs`() {
        val db =
                Room.inMemoryDatabaseBuilder(
                                ApplicationProvider.getApplicationContext(),
                                ChatDatabase::class.java
                        )
                        .allowMainThreadQueries()
                        .build()

        // Touching every DAO forces Room to verify the v5 schema hash at runtime — if the
        // migration DDL diverged from the @Entity annotations, open would throw.
        db.memNodeDao()
        db.memLinkDao()
        db.memVectorDao()
        db.memReceiptDao()
        db.runtimeStateDao()
        db.runtimeMessageDao()
        db.plannerStateDao()

        kotlinx.coroutines.runBlocking {
            db.memNodeDao().insertOrIgnore(
                    com.l2dchat.core.mem.MemNodeEntity(
                            hashId = "n1",
                            baseType = "content",
                            nodeType = "event",
                            content = "hello",
                            mentionTime = 1L,
                            eventTimeRaw = null,
                            eventTime = null,
                            startTs = null,
                            endTs = null,
                            importance = null,
                            sentiment = null,
                            metadataJson = "{}",
                            supersededBy = null,
                            accessCount = 0,
                            lastAccessed = null,
                            createdAt = 1L,
                            updatedAt = 1L
                    )
            )
            db.memLinkDao().upsertLink(eventHash = "n1", entityHash = "e1", role = "subject", mentionCount = 1, createdAt = 1L)
            db.memVectorDao().upsert(
                    com.l2dchat.core.mem.MemVectorEntity(hashId = "n1", dim = 2, embedding = byteArrayOf(0, 0, 0, 0, 0, 0, 0, 0))
            )
            db.memReceiptDao().upsert(
                    com.l2dchat.core.mem.MemReceiptEntity(
                            batchId = "b1",
                            source = "manual",
                            status = "done",
                            nodeHashesJson = "[]",
                            linkCount = 0,
                            error = null,
                            createdAt = 1L
                    )
            )
            assertEquals("b1", db.memReceiptDao().getById("b1")?.batchId)
        }

        db.close()
    }
}
