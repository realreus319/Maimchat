package com.l2dchat.core.mem

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertArrayEquals
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
 * Room DAO tests for the mem four-table schema.
 *
 * `ChatDatabase` does NOT yet register the mem entities (that wiring is wave
 * B / T1.3), so these tests run against a throwaway test-only [MemTestDb]
 * declared below. [Room.inMemoryContextBuilder] spins up a real SQLite engine
 * under Robolectric, so the SQL semantics under test — `INSERT OR IGNORE`,
 * `ON CONFLICT … DO UPDATE`, half-open interval overlap, `LIKE prefix||'%'` —
 * are exercised against the actual storage engine, not a fake.
 *
 * Coverage (per task T1.2):
 * - [MemNodeDao.insertOrIgnore] no-overwrite on PK conflict
 * - [MemLinkDao.upsertLink] `mention_count` accumulation on PK conflict
 * - [MemNodeDao.scanByTimeOverlap] half-open interval boundary semantics
 * - [MemNodeDao.resolveHashPrefix] prefix matching
 * - [MemVectorEntity] `equals` / `hashCode` content semantics (merged here
 *   because the entity is only meaningfully exercised alongside its DAO)
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class MemDaosTest {

    private lateinit var db: MemTestDb
    private lateinit var nodeDao: MemNodeDao
    private lateinit var linkDao: MemLinkDao
    private lateinit var vectorDao: MemVectorDao
    private lateinit var receiptDao: MemReceiptDao

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
                ApplicationProvider.getApplicationContext(),
                MemTestDb::class.java
        ).allowMainThreadQueries().build()
        nodeDao = db.memNodeDao()
        linkDao = db.memLinkDao()
        vectorDao = db.memVectorDao()
        receiptDao = db.memReceiptDao()
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // MemNodeDao
    // ------------------------------------------------------------------

    @Test
    fun `insertOrIgnore does not overwrite an existing row on PK conflict`() = runBlocking {
        val original = node(
                hashId = "abc123",
                content = "original content",
                mentionTime = 1_000L
        )
        nodeDao.insertOrIgnore(original)

        // Same hashId, different content — must be dropped silently.
        val conflicting = node(
                hashId = "abc123",
                content = "SHOULD NOT PERSIST",
                mentionTime = 2_000L
        )
        nodeDao.insertOrIgnore(conflicting)

        val stored = nodeDao.getById("abc123")
        assertNotNull(stored)
        assertEquals("original content", stored!!.content)
        assertEquals(1_000L, stored.mentionTime)
    }

    @Test
    fun `getById returns null for unknown hash`() = runBlocking {
        assertNull(nodeDao.getById("does-not-exist"))
    }

    @Test
    fun `getAllLiveEntities returns only non-superseded entity rows`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "evt1", baseType = "content", nodeType = "event"))
        nodeDao.insertOrIgnore(node(hashId = "ent1", baseType = "entity", nodeType = "entity"))
        nodeDao.insertOrIgnore(node(hashId = "ent2", baseType = "entity", nodeType = "entity"))
        // Supersede ent2 — it must be filtered out.
        nodeDao.setSuperseded("ent2", by = "ent3", now = 5L)

        val live = nodeDao.getAllLiveEntities()
        assertEquals(listOf("ent1"), live.map { it.hashId })
    }

    @Test
    fun `scanByType filters by node_type and excludes superseded rows`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "e1", nodeType = "event", mentionTime = 10L))
        nodeDao.insertOrIgnore(node(hashId = "e2", nodeType = "event", mentionTime = 20L))
        nodeDao.insertOrIgnore(node(hashId = "e3", nodeType = "event", mentionTime = 30L))
        nodeDao.insertOrIgnore(node(hashId = "lc1", nodeType = "life_capture", mentionTime = 40L))
        nodeDao.setSuperseded("e3", by = "e4", now = 99L)

        val events = nodeDao.scanByType(nodeType = "event", limit = 10)
        // Newest-mention first; superseded e3 excluded.
        assertEquals(listOf("e2", "e1"), events.map { it.hashId })
    }

    @Test
    fun `scanByMentionRange is half-open on both ends`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "at_start", mentionTime = 100L))
        nodeDao.insertOrIgnore(node(hashId = "inside", mentionTime = 150L))
        nodeDao.insertOrIgnore(node(hashId = "at_end", mentionTime = 200L))

        // [100, 200) — at_start included, at_end excluded.
        val rows = nodeDao.scanByMentionRange(start = 100L, end = 200L, limit = 10)
        assertEquals(setOf("at_start", "inside"), rows.map { it.hashId }.toSet())
    }

    @Test
    fun `scanByTimeOverlap matches only truly overlapping half-open intervals`() = runBlocking {
        // Node interval [100, 200)
        nodeDao.insertOrIgnore(node(hashId = "n1", startTs = 100L, endTs = 200L, mentionTime = 1L))
        // Node interval [300, 400)
        nodeDao.insertOrIgnore(node(hashId = "n2", startTs = 300L, endTs = 400L, mentionTime = 2L))
        // Node interval [200, 300) — adjacent to n1 on the right, adjacent to n2 on the left.
        nodeDao.insertOrIgnore(node(hashId = "n3", startTs = 200L, endTs = 300L, mentionTime = 3L))

        // Query [150, 250): overlaps n1 ([100,200) ∩ [150,250) = [150,200)) and n3
        // ([200,300) ∩ [150,250) = [200,250)). Does NOT overlap n2.
        val midOverlap = nodeDao.scanByTimeOverlap(qStart = 150L, qEnd = 250L, limit = 10)
        assertEquals(setOf("n1", "n3"), midOverlap.map { it.hashId }.toSet())

        // Boundary: query [200, 300) must NOT match n1 (n1 ends at 200, query starts at 200 —
        // half-open, no overlap). It matches n3 exactly and not n2 (n2 starts at 300).
        val touchingOnly = nodeDao.scanByTimeOverlap(qStart = 200L, qEnd = 300L, limit = 10)
        assertEquals(setOf("n3"), touchingOnly.map { it.hashId }.toSet())

        // Query entirely before any node — no matches.
        val beforeAll = nodeDao.scanByTimeOverlap(qStart = 0L, qEnd = 50L, limit = 10)
        assertTrue(beforeAll.isEmpty())
    }

    @Test
    fun `scanByTimeOverlap ignores superseded rows`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "live", startTs = 100L, endTs = 300L, mentionTime = 1L))
        nodeDao.insertOrIgnore(node(hashId = "gone", startTs = 100L, endTs = 300L, mentionTime = 2L))
        nodeDao.setSuperseded("gone", by = "live", now = 99L)

        val rows = nodeDao.scanByTimeOverlap(qStart = 150L, qEnd = 250L, limit = 10)
        assertEquals(listOf("live"), rows.map { it.hashId })
    }

    @Test
    fun `scanByEntityHash joins through mem_event_entity_links`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "evtA", baseType = "content", nodeType = "event", mentionTime = 10L))
        nodeDao.insertOrIgnore(node(hashId = "evtB", baseType = "content", nodeType = "event", mentionTime = 20L))
        linkDao.upsertLink(eventHash = "evtA", entityHash = "entX", role = "subject", mentionCount = 1, createdAt = 1L)
        linkDao.upsertLink(eventHash = "evtB", entityHash = "entX", role = "subject", mentionCount = 1, createdAt = 2L)
        linkDao.upsertLink(eventHash = "evtB", entityHash = "entY", role = "object", mentionCount = 1, createdAt = 3L)

        val eventsOfX = nodeDao.scanByEntityHash(entityHash = "entX", limit = 10)
        assertEquals(setOf("evtA", "evtB"), eventsOfX.map { it.hashId }.toSet())

        val eventsOfY = nodeDao.scanByEntityHash(entityHash = "entY", limit = 10)
        assertEquals(listOf("evtB"), eventsOfY.map { it.hashId })
    }

    @Test
    fun `resolveHashPrefix matches ids starting with the prefix`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "abcdef"))
        nodeDao.insertOrIgnore(node(hashId = "abcxyz"))
        nodeDao.insertOrIgnore(node(hashId = "abZZZZ"))
        nodeDao.insertOrIgnore(node(hashId = "zzzzzz"))

        val matches = nodeDao.resolveHashPrefix(prefix = "abc", limit = 10)
        // Ordered by hash_id ascending.
        assertEquals(listOf("abcdef", "abcxyz"), matches)

        val empty = nodeDao.resolveHashPrefix(prefix = "nope", limit = 10)
        assertTrue(empty.isEmpty())
    }

    @Test
    fun `resolveHashPrefix respects limit`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "pre1"))
        nodeDao.insertOrIgnore(node(hashId = "pre2"))
        nodeDao.insertOrIgnore(node(hashId = "pre3"))

        val matches = nodeDao.resolveHashPrefix(prefix = "pre", limit = 2)
        assertEquals(2, matches.size)
    }

    @Test
    fun `updateMetadata overwrites metadata_json and bumps updated_at`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "m1", metadataJson = "{\"old\":true}", updatedAt = 1L))

        nodeDao.updateMetadata(hashId = "m1", json = "{\"new\":true}", now = 42L)

        val stored = nodeDao.getById("m1")!!
        assertEquals("{\"new\":true}", stored.metadataJson)
        assertEquals(42L, stored.updatedAt)
    }

    @Test
    fun `incrementAccess adds delta and stamps last_accessed`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "a1", accessCount = 5, lastAccessed = 1L))

        nodeDao.incrementAccess(hashId = "a1", delta = 3, now = 99L)

        val stored = nodeDao.getById("a1")!!
        assertEquals(8, stored.accessCount)
        assertEquals(99L, stored.lastAccessed)
    }

    @Test
    fun `deleteById removes the row and returns rows affected`() = runBlocking {
        nodeDao.insertOrIgnore(node(hashId = "d1"))

        val affected = nodeDao.deleteById("d1")
        assertEquals(1, affected)
        assertNull(nodeDao.getById("d1"))

        val secondCall = nodeDao.deleteById("d1")
        assertEquals(0, secondCall)
    }

    // ------------------------------------------------------------------
    // MemLinkDao
    // ------------------------------------------------------------------

    @Test
    fun `upsertLink accumulates mention_count on PK conflict`() = runBlocking {
        linkDao.upsertLink(eventHash = "e1", entityHash = "ent1", role = "subject", mentionCount = 1, createdAt = 1L)
        // Same (event, entity, role) triple — must add, not replace.
        linkDao.upsertLink(eventHash = "e1", entityHash = "ent1", role = "subject", mentionCount = 2, createdAt = 2L)
        // Different role — separate row.
        linkDao.upsertLink(eventHash = "e1", entityHash = "ent1", role = "object", mentionCount = 1, createdAt = 3L)

        val links = linkDao.linksOfEvent("e1")
        assertEquals(2, links.size)
        val subject = links.single { it.role == "subject" }
        assertEquals(3, subject.mentionCount) // 1 + 2
        val obj = links.single { it.role == "object" }
        assertEquals(1, obj.mentionCount)
    }

    @Test
    fun `linksOfEntity returns every edge pointing at the entity`() = runBlocking {
        linkDao.upsertLink(eventHash = "e1", entityHash = "ent1", role = "subject", mentionCount = 1, createdAt = 1L)
        linkDao.upsertLink(eventHash = "e2", entityHash = "ent1", role = "object", mentionCount = 1, createdAt = 2L)
        linkDao.upsertLink(eventHash = "e3", entityHash = "ent2", role = "subject", mentionCount = 1, createdAt = 3L)

        val ofEnt1 = linkDao.linksOfEntity("ent1")
        assertEquals(setOf("e1", "e2"), ofEnt1.map { it.eventHash }.toSet())
    }

    @Test
    fun `deleteByHash removes edges referencing the hash on either side`() = runBlocking {
        linkDao.upsertLink(eventHash = "e1", entityHash = "ent1", role = "subject", mentionCount = 1, createdAt = 1L)
        linkDao.upsertLink(eventHash = "e2", entityHash = "ent1", role = "object", mentionCount = 1, createdAt = 2L)
        linkDao.upsertLink(eventHash = "e1", entityHash = "ent2", role = "subject", mentionCount = 1, createdAt = 3L)

        // Delete everything touching ent1 (either as event or entity).
        linkDao.deleteByHash("ent1")

        val remaining = linkDao.linksOfEvent("e1") + linkDao.linksOfEvent("e2") + linkDao.linksOfEvent("e3")
        // Only the e1↔ent2 edge survives.
        assertEquals(1, remaining.size)
        assertEquals("ent2", remaining.single().entityHash)
    }

    // ------------------------------------------------------------------
    // MemVectorDao
    // ------------------------------------------------------------------

    @Test
    fun `vector upsert replaces on conflict and loadAll returns every row`() = runBlocking {
        vectorDao.upsert(MemVectorEntity(hashId = "v1", dim = 2, embedding = byteArrayOf(0, 0, 0, 0)))
        // Replace v1 with a different blob.
        vectorDao.upsert(MemVectorEntity(hashId = "v1", dim = 2, embedding = byteArrayOf(1, 2, 3, 4)))
        vectorDao.upsert(MemVectorEntity(hashId = "v2", dim = 2, embedding = byteArrayOf(5, 6, 7, 8)))

        val all = vectorDao.loadAll()
        assertEquals(2, all.size)
        val v1 = all.single { it.hashId == "v1" }
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), v1.embedding)
    }

    @Test
    fun `vector deleteById removes only the named row`() = runBlocking {
        vectorDao.upsert(MemVectorEntity(hashId = "v1", dim = 1, embedding = byteArrayOf(0)))
        vectorDao.upsert(MemVectorEntity(hashId = "v2", dim = 1, embedding = byteArrayOf(1)))

        vectorDao.deleteById("v1")

        val remaining = vectorDao.loadAll()
        assertEquals(listOf("v2"), remaining.map { it.hashId })
    }

    // ------------------------------------------------------------------
    // MemReceiptDao
    // ------------------------------------------------------------------

    @Test
    fun `receipt upsert replaces on conflict`() = runBlocking {
        receiptDao.upsert(receipt(batchId = "b1", status = "failed", error = "boom"))
        receiptDao.upsert(receipt(batchId = "b1", status = "done", error = null))

        val stored = receiptDao.getById("b1")!!
        assertEquals("done", stored.status)
        assertNull(stored.error)
    }

    @Test
    fun `findDoneBatchIds returns only done receipts among the given ids`() = runBlocking {
        receiptDao.upsert(receipt(batchId = "b1", status = "done"))
        receiptDao.upsert(receipt(batchId = "b2", status = "failed"))
        receiptDao.upsert(receipt(batchId = "b3", status = "done"))

        val done = receiptDao.findDoneBatchIds(listOf("b1", "b2", "b3", "b4"))
        assertEquals(setOf("b1", "b3"), done.toSet())
    }

    @Test
    fun `getById returns null for unknown batch`() = runBlocking {
        assertNull(receiptDao.getById("never"))
    }

    // ------------------------------------------------------------------
    // MemVectorEntity equals / hashCode (content semantics)
    // ------------------------------------------------------------------

    @Test
    fun `MemVectorEntity equals compares embedding by content not reference`() {
        val a = MemVectorEntity(hashId = "h", dim = 2, embedding = byteArrayOf(1, 2, 3, 4))
        val b = MemVectorEntity(hashId = "h", dim = 2, embedding = byteArrayOf(1, 2, 3, 4))
        val c = MemVectorEntity(hashId = "h", dim = 2, embedding = byteArrayOf(9, 9, 9, 9))

        assertEquals(a, b)
        assertEquals(a.hashCode(), b.hashCode())
        assertFalse(a == c)
    }

    @Test
    fun `MemVectorEntity equals distinguishes by hashId and dim`() {
        val base = MemVectorEntity(hashId = "h", dim = 2, embedding = byteArrayOf(1, 2, 3, 4))
        assertFalse(base == MemVectorEntity(hashId = "other", dim = 2, embedding = byteArrayOf(1, 2, 3, 4)))
        assertFalse(base == MemVectorEntity(hashId = "h", dim = 4, embedding = byteArrayOf(1, 2, 3, 4)))
    }

    @Test
    fun `MemVectorEntity equals is reflexive and handles non-MemVectorEntity`() {
        val a = MemVectorEntity(hashId = "h", dim = 1, embedding = byteArrayOf(0))
        assertEquals(a, a)
        assertFalse(a.equals("not a vector"))
        assertFalse(a.equals(null))
    }

    @Test
    fun `MemVectorEntity survives round-trip through DAO with byte-identical blob`() = runBlocking {
        val original = MemVectorEntity(hashId = "rt", dim = 4, embedding = byteArrayOf(0x00, 0x7F, 0x80.toByte(), 0xFF.toByte()))
        vectorDao.upsert(original)

        val loaded = vectorDao.loadAll().single { it.hashId == "rt" }
        // Content-equality (not reference) — this is the whole point of overriding equals.
        assertEquals(original, loaded)
        assertEquals(original.hashCode(), loaded.hashCode())
        assertArrayEquals(original.embedding, loaded.embedding)
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    /** Minimal [MemNodeEntity] with sensible defaults; override only what each test cares about. */
    private fun node(
            hashId: String = "h",
            baseType: String = "content",
            nodeType: String = "event",
            content: String = "c",
            mentionTime: Long = 0L,
            eventTimeRaw: String? = null,
            eventTime: String? = null,
            startTs: Long? = null,
            endTs: Long? = null,
            importance: Double? = null,
            sentiment: Double? = null,
            metadataJson: String = "{}",
            supersededBy: String? = null,
            accessCount: Int = 0,
            lastAccessed: Long? = null,
            createdAt: Long = 0L,
            updatedAt: Long = 0L
    ): MemNodeEntity = MemNodeEntity(
            hashId = hashId,
            baseType = baseType,
            nodeType = nodeType,
            content = content,
            mentionTime = mentionTime,
            eventTimeRaw = eventTimeRaw,
            eventTime = eventTime,
            startTs = startTs,
            endTs = endTs,
            importance = importance,
            sentiment = sentiment,
            metadataJson = metadataJson,
            supersededBy = supersededBy,
            accessCount = accessCount,
            lastAccessed = lastAccessed,
            createdAt = createdAt,
            updatedAt = updatedAt
    )

    private fun receipt(
            batchId: String = "b",
            source: String? = "chat_compact",
            status: String = "done",
            nodeHashesJson: String = "[]",
            linkCount: Int = 0,
            error: String? = null,
            createdAt: Long = 0L
    ): MemReceiptEntity = MemReceiptEntity(
            batchId = batchId,
            source = source,
            status = status,
            nodeHashesJson = nodeHashesJson,
            linkCount = linkCount,
            error = error,
            createdAt = createdAt
    )
}

/**
 * Throwaway test-only [RoomDatabase] that registers ONLY the four mem entities.
 *
 * `ChatDatabase` does not yet include these entities (registration is wave B /
 * T1.3), so the production database cannot host these DAOs. This private
 * database gives the tests a real SQLite engine to exercise the DAO SQL
 * semantics (`INSERT OR IGNORE`, `ON CONFLICT … DO UPDATE`, `LIKE`, interval
 * overlap) without depending on the unfinished migration.
 */
@Database(
        entities = [
            MemNodeEntity::class,
            MemEventEntityLinkEntity::class,
            MemVectorEntity::class,
            MemReceiptEntity::class
        ],
        version = 1,
        exportSchema = false
)
abstract class MemTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao

    abstract fun memLinkDao(): MemLinkDao

    abstract fun memVectorDao(): MemVectorDao

    abstract fun memReceiptDao(): MemReceiptDao
}
