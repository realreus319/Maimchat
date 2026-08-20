package com.l2dchat.core.mem

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.test.core.app.ApplicationProvider
import java.time.ZoneId
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Storage-engine tests for [MemStore] + [TxWriter] (plan task T1.6).
 *
 * These tests run against a throwaway test-only [MemStoreTestDb] (same pattern
 * as [MemDaosTest]'s `MemTestDb`) so they do not depend on the unfinished
 * `ChatDatabase` migration. [Room.inMemoryDatabaseBuilder] spins up a real
 * SQLite engine under Robolectric, so the SQL semantics under test — `INSERT
 * OR IGNORE`, `ON CONFLICT … DO UPDATE`, half-open interval overlap — are
 * exercised against the actual storage engine.
 *
 * Coverage (per task T1.6 MUST-DO list):
 * - Idempotent replay: same ops twice → identical DB state.
 * - Receipt-done skip: a batch whose receipt is already `done` is a no-op.
 * - Link PK dedup with `mention_count` accumulation.
 * - Supersede filtering in [MemStore.getNode] default vs `includeSuperseded`.
 * - Vector dim mismatch → [DimMismatchError].
 * - BM25 index maintenance across put/supersede/delete.
 * - [MemStore.findEntities] canonical + alias, case-insensitive.
 * - [MemStore.scanNodes] timeOverlap boundary.
 * - [MemStore.deleteNode] cascades to links + vectors.
 * - [MemStore.recordAccess] increments.
 *
 * Plus [TxWriter]-specific coverage:
 * - [TxWriter.putNode] returns the content-addressed hash.
 * - [TxWriter.putVector] rejects dim mismatch up front.
 * - [TxWriter.updateNode] rejects unknown fields.
 * - [TxWriter.flush] order and staging-clear-on-success.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class MemStoreTest {

    private lateinit var db: MemStoreTestDb
    private lateinit var nodeDao: MemNodeDao
    private lateinit var linkDao: MemLinkDao
    private lateinit var vectorDao: MemVectorDao
    private lateinit var receiptDao: MemReceiptDao
    private lateinit var store: MemStore

    private val zone: ZoneId = ZoneId.of("Asia/Shanghai")

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            MemStoreTestDb::class.java,
        ).allowMainThreadQueries().build()
        nodeDao = db.memNodeDao()
        linkDao = db.memLinkDao()
        vectorDao = db.memVectorDao()
        receiptDao = db.memReceiptDao()
        store = MemStore(
            nodeDao = nodeDao,
            linkDao = linkDao,
            vectorDao = vectorDao,
            receiptDao = receiptDao,
            expectedDim = 3,
            // transactionRunner left null: each DAO call runs in its own
            // implicit Room transaction. The tests exercise individual op
            // semantics and idempotent-replay safety, neither of which requires
            // cross-op atomicity (every op is itself idempotent: INSERT OR
            // IGNORE, ON CONFLICT DO UPDATE, INSERT OR REPLACE).
            transactionRunner = null,
            zoneId = zone,
        )
        runBlocking { store.load() }
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // TxWriter: staging semantics
    // ------------------------------------------------------------------

    @Test
    fun `TxWriter putNode returns the content-addressed hash`() {
        val writer = TxWriter(dimensions = 3)
        val hashId = writer.putNode(
            nodeType = "event",
            content = "用户提到了项目X",
            eventTime = "2026-08-08",
        )
        // The hash must match Hashing.shortHash(nodeType + content + eventTime),
        // mirroring the Python tx.py composition.
        assertEquals(
            Hashing.shortHash("event用户提到了项目X2026-08-08"),
            hashId,
        )
        assertEquals(12, hashId.length)
    }

    @Test
    fun `TxWriter putNode with null eventTime hashes nodeType + content + empty string`() {
        val writer = TxWriter(dimensions = 3)
        val hashId = writer.putNode(
            nodeType = "entity",
            content = "项目X",
            eventTime = null,
        )
        assertEquals(Hashing.shortHash("entity项目X"), hashId)
    }

    @Test
    fun `TxWriter putNode deduplicates on hash and never overwrites`() {
        val writer = TxWriter(dimensions = 3)
        val h1 = writer.putNode(nodeType = "event", content = "c", eventTime = "2026-08-08")
        // Same content → same hash → the second call is a no-op.
        val h2 = writer.putNode(nodeType = "event", content = "c", eventTime = "2026-08-08")
        assertEquals(h1, h2)
        assertEquals(1, writer.size())
    }

    @Test
    fun `TxWriter putVector rejects dimension mismatch up front`() {
        val writer = TxWriter(dimensions = 3)
        var threw = false
        try {
            writer.putVector("h", listOf(1f, 2f))
        } catch (e: DimMismatchError) {
            threw = true
            assertTrue(e.message!!.contains("expected 3"))
            assertTrue(e.message!!.contains("got 2"))
        }
        assertTrue("putVector must throw DimMismatchError on dim mismatch", threw)
    }

    @Test
    fun `TxWriter updateNode rejects unknown fields`() {
        val writer = TxWriter(dimensions = 3)
        var threw = false
        try {
            writer.updateNode("h", mapOf("content" to "new"))
        } catch (e: ToolValidationError) {
            threw = true
            assertTrue(e.message!!.contains("not updatable"))
        }
        assertTrue("updateNode must reject fields outside the whitelist", threw)
    }

    @Test
    fun `TxWriter putLink deduplicates and bumps mention_count`() {
        val writer = TxWriter(dimensions = 3)
        val first = writer.putLink("evt", "ent", "subject")
        val second = writer.putLink("evt", "ent", "subject")
        assertTrue(first)
        assertFalse(second)
        // One staged link, mention_count = 2.
        val ops = writer.stagedOps().filterIsInstance<TxOp.PutLink>()
        assertEquals(1, ops.size)
        assertEquals(2, ops[0].mentionCount)
    }

    @Test
    fun `TxWriter flush order is put_node, update_node, put_link, put_vector, put_receipt`() = runBlocking {
        val writer = TxWriter(dimensions = 3)
        val h = writer.putNode(nodeType = "event", content = "c", mentionTime = 100L)
        writer.updateNode(h, mapOf("access_count" to 1))
        writer.putLink(h, "ent", "subject")
        writer.putVector(h, listOf(1f, 0f, 0f))
        writer.putReceipt(batchId = "b1", source = "chat_compact", status = "done")

        val ops = writer.stagedOps()
        assertEquals(5, ops.size)
        assertTrue(ops[0] is TxOp.PutNode)
        assertTrue(ops[1] is TxOp.UpdateNode)
        assertTrue(ops[2] is TxOp.PutLink)
        assertTrue(ops[3] is TxOp.PutVector)
        assertTrue(ops[4] is TxOp.PutReceipt)

        writer.flush(store)
        assertEquals(0, writer.size())
    }

    @Test
    fun `TxWriter flush on empty staging is a no-op`() = runBlocking {
        val writer = TxWriter(dimensions = 3)
        writer.flush(store) // must not throw
    }

    // ------------------------------------------------------------------
    // MemStore apply: idempotent replay + receipt-done skip
    // ------------------------------------------------------------------

    @Test
    fun `idempotent replay - same batch applied twice leaves identical DB state`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(nodeType = "event", content = "replay-test", mentionTime = 1_000L)
        writer.putLink(h, "entA", "subject")
        writer.putReceipt(batchId = "batch-replay-1", source = "chat_compact", status = "done")
        writer.flush(store)

        val nodeAfterFirst = store.getNode(h)
        val linksAfterFirst = store.getLinks(eventHash = h)
        val receiptAfterFirst = store.getReceipt("batch-replay-1")
        assertNotNull(nodeAfterFirst)
        assertEquals(1, linksAfterFirst.size)
        assertNotNull(receiptAfterFirst)

        // Re-flush the same batch — every op must be a no-op.
        val writer2 = freshWriter()
        writer2.putNode(nodeType = "event", content = "replay-test", mentionTime = 1_000L)
        writer2.putLink(h, "entA", "subject")
        writer2.putReceipt(batchId = "batch-replay-1", source = "chat_compact", status = "done")
        writer2.flush(store)

        val nodeAfterSecond = store.getNode(h)
        val linksAfterSecond = store.getLinks(eventHash = h)
        val receiptAfterSecond = store.getReceipt("batch-replay-1")

        assertEquals(nodeAfterFirst, nodeAfterSecond)
        assertEquals(linksAfterFirst.size, linksAfterSecond.size)
        assertEquals(
            linksAfterFirst[0].mentionCount,
            linksAfterSecond[0].mentionCount,
        )
        assertEquals(receiptAfterFirst, receiptAfterSecond)
    }

    @Test
    fun `receipt-done skip makes apply a no-op even when other ops would mutate`() = runBlocking {
        // Seed a done receipt.
        val seed = freshWriter()
        seed.putReceipt(batchId = "b-skip", source = "chat_compact", status = "done")
        seed.flush(store)
        assertNotNull(store.getReceipt("b-skip"))

        // Now stage a batch that includes the same done receipt PLUS a new node.
        // The whole apply must be skipped.
        val writer = freshWriter()
        writer.putNode(nodeType = "event", content = "should-not-persist", mentionTime = 1L)
        writer.putReceipt(batchId = "b-skip", source = "chat_compact", status = "done")
        writer.flush(store)

        // The new node must NOT exist.
        val expectedHash = Hashing.shortHash("eventshould-not-persist")
        assertNull(store.getNode(expectedHash))
    }

    @Test
    fun `failed receipt does not block re-application`() = runBlocking {
        // First batch fails.
        val first = freshWriter()
        first.putNode(nodeType = "event", content = "retry-content", mentionTime = 5L)
        first.putReceipt(batchId = "b-retry", source = "chat_compact", status = "failed", error = "boom")
        first.flush(store)

        val receiptAfterFail = store.getReceipt("b-retry")!!
        assertEquals(ReceiptStatus.FAILED, receiptAfterFail.status)

        // Retry the same batch — the receipt is not `done`, so apply runs and
        // overwrites the receipt with `done`.
        val retry = freshWriter()
        retry.putNode(nodeType = "event", content = "retry-content", mentionTime = 5L)
        retry.putReceipt(batchId = "b-retry", source = "chat_compact", status = "done")
        retry.flush(store)

        val receiptAfterRetry = store.getReceipt("b-retry")!!
        assertEquals(ReceiptStatus.DONE, receiptAfterRetry.status)
    }

    // ------------------------------------------------------------------
    // Link PK dedup with mention_count accumulation
    // ------------------------------------------------------------------

    @Test
    fun `putLink accumulates mention_count across separate batches`() = runBlocking {
        val h = Hashing.shortHash("eventlink-acc")
        val ent = Hashing.shortHash("entitytarget")

        val w1 = freshWriter()
        w1.putNode(nodeType = "event", content = "link-acc", mentionTime = 1L)
        w1.putLink(h, ent, "subject")
        w1.flush(store)

        val w2 = freshWriter()
        w2.putLink(h, ent, "subject")
        w2.flush(store)

        val links = store.getLinks(eventHash = h)
        assertEquals(1, links.size)
        assertEquals(2, links[0].mentionCount)
    }

    // ------------------------------------------------------------------
    // Supersede filtering
    // ------------------------------------------------------------------

    @Test
    fun `getNode hides superseded nodes by default`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(nodeType = "entity", content = "old-card", mentionTime = 1L)
        writer.flush(store)

        // Supersede it.
        val supersedW = freshWriter()
        supersedW.updateNode(h, mapOf("superseded_by" to "new-hash"))
        supersedW.flush(store)

        assertNull("superseded node must be hidden by default", store.getNode(h))
        assertNotNull(
            "includeSuperseded must return it",
            store.getNode(h, includeSuperseded = true),
        )
    }

    // ------------------------------------------------------------------
    // Vector dim mismatch
    // ------------------------------------------------------------------

    @Test
    fun `putVector with wrong dimension throws DimMismatchError`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(nodeType = "event", content = "dim-test", mentionTime = 1L)
        writer.flush(store)

        // Store expects dim=3 (set in setUp). TxWriter.putVector validates dim
        // immediately at staging time (matching Python tx.py put_vector), so the
        // exception is thrown by putVector — not deferred to flush.
        val badWriter = freshWriter()
        var threw = false
        try {
            badWriter.putVector(h, listOf(1f, 2f))
        } catch (e: DimMismatchError) {
            threw = true
        }
        assertTrue("putVector must throw DimMismatchError on dim mismatch", threw)
    }

    @Test
    fun `vecTopK returns cosine-ranked results after putVector`() = runBlocking {
        val writer = freshWriter()
        val h1 = writer.putNode(nodeType = "event", content = "v1", mentionTime = 1L)
        val h2 = writer.putNode(nodeType = "event", content = "v2", mentionTime = 2L)
        writer.putVector(h1, listOf(1f, 0f, 0f))
        writer.putVector(h2, listOf(0f, 1f, 0f))
        writer.flush(store)

        val results = store.vecTopK(floatArrayOf(1f, 0f, 0f), k = 2)
        assertEquals(2, results.size)
        // h1 is parallel to the query → score 1.0; h2 is orthogonal → score 0.0.
        assertEquals(h1, results[0].first)
        assertEquals(1.0, results[0].second, 1e-6)
        assertEquals(h2, results[1].first)
        assertEquals(0.0, results[1].second, 1e-6)
    }

    // ------------------------------------------------------------------
    // BM25 index maintenance across put/supersede/delete
    // ------------------------------------------------------------------

    @Test
    fun `entityLexTopK reflects newly added entity cards`() = runBlocking {
        val writer = freshWriter()
        writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "罗联政府",
            mentionTime = 1L,
            metadata = mapOf("canonical_name" to "罗联政府", "aliases" to listOf("罗联")),
        )
        writer.flush(store)

        val hits = store.entityLexTopK("罗联", k = 5)
        assertEquals(1, hits.size)
        assertTrue(hits[0].first.isNotEmpty())
        assertTrue(hits[0].second > 0.0)
    }

    @Test
    fun `entityLexTopK drops superseded entity cards`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "临时实体",
            mentionTime = 1L,
            metadata = mapOf("canonical_name" to "临时实体"),
        )
        writer.flush(store)
        assertEquals(1, store.entityLexTopK("临时", k = 5).size)

        // Supersede it — the BM25 index must drop it.
        val supersedW = freshWriter()
        supersedW.updateNode(h, mapOf("superseded_by" to "replacement"))
        supersedW.flush(store)

        assertEquals(0, store.entityLexTopK("临时", k = 5).size)
    }

    @Test
    fun `entityLexTopK drops deleted entity cards`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "删除实体",
            mentionTime = 1L,
            metadata = mapOf("canonical_name" to "删除实体"),
        )
        writer.flush(store)
        assertEquals(1, store.entityLexTopK("删除", k = 5).size)

        store.deleteNode(h)

        assertEquals(0, store.entityLexTopK("删除", k = 5).size)
    }

    // ------------------------------------------------------------------
    // findEntities: canonical + alias, case-insensitive
    // ------------------------------------------------------------------

    @Test
    fun `findEntities matches canonical name case-insensitively`() = runBlocking {
        val writer = freshWriter()
        writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "Alice",
            mentionTime = 1L,
            metadata = mapOf("canonical_name" to "Alice"),
        )
        writer.flush(store)

        assertEquals(1, store.findEntities("Alice").size)
        assertEquals(1, store.findEntities("alice").size)
        assertEquals(1, store.findEntities("ALICE").size)
        assertEquals(0, store.findEntities("Bob").size)
    }

    @Test
    fun `findEntities matches aliases`() = runBlocking {
        val writer = freshWriter()
        writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "KFC",
            mentionTime = 1L,
            metadata = mapOf(
                "canonical_name" to "Kentucky Fried Chicken",
                "aliases" to listOf("KFC", "肯德基"),
            ),
        )
        writer.flush(store)

        assertEquals(1, store.findEntities("KFC").size)
        assertEquals(1, store.findEntities("kfc").size)
        assertEquals(1, store.findEntities("肯德基").size)
        assertEquals(1, store.findEntities("Kentucky Fried Chicken").size)
    }

    @Test
    fun `findEntities ignores superseded entities`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "旧名",
            mentionTime = 1L,
            metadata = mapOf("canonical_name" to "旧名"),
        )
        writer.flush(store)
        assertEquals(1, store.findEntities("旧名").size)

        val supersedW = freshWriter()
        supersedW.updateNode(h, mapOf("superseded_by" to "新名"))
        supersedW.flush(store)

        assertEquals(0, store.findEntities("旧名").size)
    }

    @Test
    fun `findEntities empty query returns empty`() = runBlocking {
        val writer = freshWriter()
        writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "X",
            mentionTime = 1L,
            metadata = mapOf("canonical_name" to "X"),
        )
        writer.flush(store)

        assertTrue(store.findEntities("").isEmpty())
        assertTrue(store.findEntities("   ").isEmpty())
    }

    // ------------------------------------------------------------------
    // scanNodes: timeOverlap boundary
    // ------------------------------------------------------------------

    @Test
    fun `scanNodes timeOverlap matches only truly overlapping intervals`() = runBlocking {
        // Insert nodes directly via the DAO with exact epoch start_ts/end_ts
        // so the boundary semantics are not affected by event_time parsing.
        nodeDao.insertOrIgnore(memNode(hashId = "n1", startTs = 100L, endTs = 200L, mentionTime = 1L))
        nodeDao.insertOrIgnore(memNode(hashId = "n2", startTs = 300L, endTs = 400L, mentionTime = 2L))
        nodeDao.insertOrIgnore(memNode(hashId = "n3", startTs = 200L, endTs = 300L, mentionTime = 3L))

        // Query [150, 250): overlaps n1 and n3, not n2.
        val midOverlap = store.scanNodes(timeOverlap = 150L to 250L, limit = 10)
        assertEquals(setOf("n1", "n3"), midOverlap.map { it.hashId }.toSet())

        // Boundary: query [200, 300) must NOT match n1 (half-open).
        val touchingOnly = store.scanNodes(timeOverlap = 200L to 300L, limit = 10)
        assertEquals(setOf("n3"), touchingOnly.map { it.hashId }.toSet())
    }

    @Test
    fun `scanNodes filters by nodeType`() = runBlocking {
        nodeDao.insertOrIgnore(memNode(hashId = "e1", nodeType = "event", mentionTime = 10L))
        nodeDao.insertOrIgnore(memNode(hashId = "e2", nodeType = "event", mentionTime = 20L))
        nodeDao.insertOrIgnore(memNode(hashId = "lc1", nodeType = "life_capture", mentionTime = 30L))

        val events = store.scanNodes(nodeType = "event", limit = 10)
        assertEquals(setOf("e1", "e2"), events.map { it.hashId }.toSet())
    }

    @Test
    fun `scanNodes filters by mentionRange half-open`() = runBlocking {
        nodeDao.insertOrIgnore(memNode(hashId = "at_start", mentionTime = 100L))
        nodeDao.insertOrIgnore(memNode(hashId = "inside", mentionTime = 150L))
        nodeDao.insertOrIgnore(memNode(hashId = "at_end", mentionTime = 200L))

        val rows = store.scanNodes(mentionRange = 100L to 200L, limit = 10)
        assertEquals(setOf("at_start", "inside"), rows.map { it.hashId }.toSet())
    }

    @Test
    fun `scanNodes filters by entityHash via link join`() = runBlocking {
        nodeDao.insertOrIgnore(memNode(hashId = "evtA", nodeType = "event", mentionTime = 10L))
        nodeDao.insertOrIgnore(memNode(hashId = "evtB", nodeType = "event", mentionTime = 20L))
        linkDao.upsertLink("evtA", "entX", "subject", 1, 1L)
        linkDao.upsertLink("evtB", "entX", "subject", 1, 2L)

        val rows = store.scanNodes(entityHash = "entX", limit = 10)
        assertEquals(setOf("evtA", "evtB"), rows.map { it.hashId }.toSet())
    }

    // ------------------------------------------------------------------
    // deleteNode cascade
    // ------------------------------------------------------------------

    @Test
    fun `deleteNode cascades to links and vectors`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(nodeType = "event", content = "cascade-target", mentionTime = 1L)
        val ent = Hashing.shortHash("entitycascade-ent")
        writer.putLink(h, ent, "subject")
        writer.putVector(h, listOf(1f, 0f, 0f))
        writer.flush(store)

        // Confirm everything is present.
        assertNotNull(store.getNode(h))
        assertEquals(1, store.getLinks(eventHash = h).size)
        assertEquals(1, vectorDao.loadAll().size)

        val deleted = store.deleteNode(h)
        assertTrue(deleted)

        assertNull(store.getNode(h, includeSuperseded = true))
        assertEquals(0, store.getLinks(eventHash = h).size)
        assertEquals(0, vectorDao.loadAll().size)
        // Vector index must also drop it.
        assertEquals(0, store.vecTopK(floatArrayOf(1f, 0f, 0f), k = 5).size)
    }

    @Test
    fun `deleteNode returns false when the node does not exist`() = runBlocking {
        assertFalse(store.deleteNode("nonexistent-hash-id"))
    }

    // ------------------------------------------------------------------
    // recordAccess
    // ------------------------------------------------------------------

    @Test
    fun `recordAccess increments access_count and stamps last_accessed`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(nodeType = "event", content = "access-target", mentionTime = 1L)
        writer.flush(store)

        val before = store.getNode(h)!!
        assertEquals(0, before.accessCount)

        store.recordAccess(h)
        store.recordAccess(h)

        val after = store.getNode(h)!!
        assertEquals(2, after.accessCount)
        assertTrue(after.lastAccessed != null)
        assertTrue(after.lastAccessed!! > 0L)
    }

    @Test
    fun `recordAccess on missing hash is a silent no-op`() = runBlocking {
        store.recordAccess("does-not-exist") // must not throw
    }

    // ------------------------------------------------------------------
    // resolveHash
    // ------------------------------------------------------------------

    @Test
    fun `resolveHash returns matching full hashes for a prefix`() = runBlocking {
        nodeDao.insertOrIgnore(memNode(hashId = "abcdef123456"))
        nodeDao.insertOrIgnore(memNode(hashId = "abcdef999999"))
        nodeDao.insertOrIgnore(memNode(hashId = "zzzzzzzzzzzz"))

        val matches = store.resolveHash("abcdef", limit = 10)
        assertEquals(listOf("abcdef123456", "abcdef999999"), matches)
    }

    @Test
    fun `resolveHash rejects prefixes shorter than 6 chars`() = runBlocking {
        nodeDao.insertOrIgnore(memNode(hashId = "abcdef123456"))
        assertEquals(emptyList<String>(), store.resolveHash("abc", limit = 10))
    }

    // ------------------------------------------------------------------
    // getReceipt
    // ------------------------------------------------------------------

    @Test
    fun `getReceipt returns null for unknown batch`() = runBlocking {
        assertNull(store.getReceipt("never-recorded"))
    }

    @Test
    fun `getReceipt round-trips node_hashes`() = runBlocking {
        val writer = freshWriter()
        writer.putReceipt(
            batchId = "b-roundtrip",
            source = "chat_compact",
            status = "done",
            nodeHashes = listOf("h1", "h2", "h3"),
            linkCount = 7,
        )
        writer.flush(store)

        val receipt = store.getReceipt("b-roundtrip")!!
        assertEquals("b-roundtrip", receipt.batchId)
        assertEquals("chat_compact", receipt.source)
        assertEquals(ReceiptStatus.DONE, receipt.status)
        assertEquals(listOf("h1", "h2", "h3"), receipt.nodeHashes)
        assertEquals(7, receipt.linkCount)
    }

    // ------------------------------------------------------------------
    // getLinks validation
    // ------------------------------------------------------------------

    @Test
    fun `getLinks throws when neither direction is given`() = runBlocking {
        var threw = false
        try {
            store.getLinks()
        } catch (e: ToolValidationError) {
            threw = true
        }
        assertTrue(threw)
    }

    // ------------------------------------------------------------------
    // updateNode field semantics
    // ------------------------------------------------------------------

    @Test
    fun `updateNode merges metadata maps`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "merge-target",
            mentionTime = 1L,
            metadata = mapOf("a" to 1, "b" to 2),
        )
        writer.flush(store)

        val updateW = freshWriter()
        updateW.updateNode(h, mapOf("metadata" to mapOf("b" to 20, "c" to 30)))
        updateW.flush(store)

        val node = store.getNode(h)!!
        // Metadata values round-trip through JSON as Long (the JSON parser
        // reads integer-valued numbers as Long), so compare against Long.
        assertEquals(1L, node.metadata["a"])
        assertEquals(20L, node.metadata["b"])
        assertEquals(30L, node.metadata["c"])
    }

    @Test
    fun `updateNode unions aliases`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "alias-target",
            mentionTime = 1L,
            metadata = mapOf("aliases" to listOf("A1", "A2")),
        )
        writer.flush(store)

        val updateW = freshWriter()
        updateW.updateNode(h, mapOf("aliases" to listOf("A2", "A3")))
        updateW.flush(store)

        @Suppress("UNCHECKED_CAST")
        val aliases = store.getNode(h)!!.metadata["aliases"] as List<String>
        assertEquals(listOf("A1", "A2", "A3"), aliases)
    }

    @Test
    fun `updateNode increments appearance_count inside metadata`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "entity",
            baseType = "entity",
            content = "appearance-target",
            mentionTime = 1L,
            metadata = mapOf("appearance_count" to 5),
        )
        writer.flush(store)

        val updateW = freshWriter()
        updateW.updateNode(h, mapOf("appearance_count" to 3))
        updateW.flush(store)

        val node = store.getNode(h)!!
        assertEquals(8L, node.metadata["appearance_count"])
    }

    @Test
    fun `updateNode increments access_count column`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(nodeType = "event", content = "access-target", mentionTime = 1L)
        writer.flush(store)

        val updateW = freshWriter()
        updateW.updateNode(h, mapOf("access_count" to 5))
        updateW.flush(store)

        assertEquals(5, store.getNode(h)!!.accessCount)
    }

    @Test
    fun `updateNode on missing node throws ToolValidationError`() = runBlocking {
        val writer = freshWriter()
        writer.updateNode("nonexistent", mapOf("access_count" to 1))
        var threw = false
        try {
            writer.flush(store)
        } catch (e: ToolValidationError) {
            threw = true
            assertTrue(e.message!!.contains("not found"))
        }
        assertTrue(threw)
    }

    // ------------------------------------------------------------------
    // applyPutNode time expansion
    // ------------------------------------------------------------------

    @Test
    fun `applyPutNode expands event_time into start_ts and end_ts`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "event",
            content = "time-expand",
            eventTime = "2026-08-08",
            mentionTime = 1_786_118_400L,
        )
        writer.flush(store)

        val node = store.getNode(h)!!
        // parseEventTime("2026-08-08") under Asia/Shanghai → (1786118400, 1786204800).
        assertEquals(1786118400L, node.startTs)
        assertEquals(1786204800L, node.endTs)
    }

    @Test
    fun `applyPutNode without event_time falls back to mention_time instant interval`() = runBlocking {
        val writer = freshWriter()
        val h = writer.putNode(
            nodeType = "event",
            content = "instant",
            eventTime = null,
            mentionTime = 42L,
        )
        writer.flush(store)

        val node = store.getNode(h)!!
        assertEquals(42L, node.startTs)
        assertEquals(42L, node.endTs)
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    /** Fresh writer with the same dim as the store under test. */
    private fun freshWriter(): TxWriter = TxWriter(dimensions = 3)

    /** Minimal [MemNodeEntity] with sensible defaults for scanNodes fixtures. */
    private fun memNode(
        hashId: String = "h",
        baseType: String = "content",
        nodeType: String = "event",
        content: String = "c",
        mentionTime: Long = 0L,
        startTs: Long? = null,
        endTs: Long? = null,
        metadataJson: String = "{}",
        supersededBy: String? = null,
        accessCount: Int = 0,
        lastAccessed: Long? = null,
        createdAt: Long = 0L,
        updatedAt: Long = 0L,
    ): MemNodeEntity = MemNodeEntity(
        hashId = hashId,
        baseType = baseType,
        nodeType = nodeType,
        content = content,
        mentionTime = mentionTime,
        eventTimeRaw = null,
        eventTime = null,
        startTs = startTs,
        endTs = endTs,
        importance = null,
        sentiment = null,
        metadataJson = metadataJson,
        supersededBy = supersededBy,
        accessCount = accessCount,
        lastAccessed = lastAccessed,
        createdAt = createdAt,
        updatedAt = updatedAt,
    )
}

/**
 * Throwaway test-only [RoomDatabase] that registers ONLY the four mem entities.
 *
 * Same pattern as `MemDaosTest.MemTestDb`; declared separately here so this
 * test class is self-contained and does not depend on another test file's
 * private database.
 */
@Database(
    entities = [
        MemNodeEntity::class,
        MemEventEntityLinkEntity::class,
        MemVectorEntity::class,
        MemReceiptEntity::class,
    ],
    version = 1,
    exportSchema = false,
)
abstract class MemStoreTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
