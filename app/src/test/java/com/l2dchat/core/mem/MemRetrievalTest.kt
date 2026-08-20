package com.l2dchat.core.mem

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.test.core.app.ApplicationProvider
import java.time.ZoneId
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Tests for [MemRetrieval] — the four no-LLM read APIs (plan task T5).
 *
 * Runs against a throwaway test-only [MemRetrievalTestDb] (same pattern as
 * [MemStoreTest]'s `MemStoreTestDb`) so the SQL semantics under test —
 * half-open interval overlap, link joins, supersede filtering — are exercised
 * against a real SQLite engine under Robolectric.
 *
 * The fixture is built via [TxWriter] (the production write path) with a
 * hand-written [FakeEmbedder] that returns deterministic vectors, so the
 * cosine ranking is fully reproducible.
 *
 * Coverage (per task T5 MUST-DO list):
 * - [MemRetrieval.search] basic semantic ordering.
 * - [MemRetrieval.search] with `nodeType` filter.
 * - [MemRetrieval.search] with `timeRange` on mention axis and event axis.
 * - [MemRetrieval.search] with `entity` filter.
 * - [MemRetrieval.search] `includeSuperseded` default-false vs true.
 * - [MemRetrieval.temporal] on both axes incl. boundary overlap.
 * - [MemRetrieval.entityEvents] ordering + limit.
 * - [MemRetrieval.expand] returns full content + metadata + links +
 *   supersede chain AND bumps `accessCount` (verified via [MemStore.getNode]).
 * - `k` cap enforcement.
 * - Empty-result cases.
 * - Bad `timeAxis` / `axis` raises [ToolValidationError].
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class MemRetrievalTest {

    private lateinit var db: MemRetrievalTestDb
    private lateinit var nodeDao: MemNodeDao
    private lateinit var linkDao: MemLinkDao
    private lateinit var vectorDao: MemVectorDao
    private lateinit var receiptDao: MemReceiptDao
    private lateinit var store: MemStore
    private lateinit var embedder: FakeEmbedder
    private lateinit var retrieval: MemRetrieval

    /**
     * Fixed timezone so [TimeParse.parseEventTime] results are deterministic
     * regardless of the test runner's locale. Matches [MemStoreTest]'s zone.
     */
    private val zone: ZoneId = ZoneId.of("Asia/Shanghai")

    /**
     * Fixed `now` for the rank decay/boost math, injected via [MemRetrieval]'s
     * `nowFn`. Chosen to be comfortably after every fixture `mentionTime` so
     * the mention-axis decay applies (no future-event boost unless a test
     * explicitly seeds one).
     */
    private val fixedNow: Long = 2_000_000_000L

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            MemRetrievalTestDb::class.java,
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
            transactionRunner = null,
            zoneId = zone,
        )
        runBlocking { store.load() }
        embedder = FakeEmbedder(dimension = 3)
        retrieval = MemRetrieval(
            store = store,
            embedder = embedder,
            zoneId = zone,
            nowFn = { fixedNow },
        )
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // search: basic semantic ordering
    // ------------------------------------------------------------------

    @Test
    fun `search orders results by cosine similarity best first`() = runBlocking {
        // Three events with distinct vectors. The query embeds to (1,0,0), so
        // h1 (parallel) > h2 (diagonal) > h3 (orthogonal).
        val h1 = putEvent("event-a", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        val h2 = putEvent("event-b", vector = listOf(1f, 1f, 0f), mentionTime = 1_000L)
        val h3 = putEvent("event-c", vector = listOf(0f, 1f, 0f), mentionTime = 1_000L)
        embedder.queue(listOf(1f, 0f, 0f))

        val results = retrieval.search("query", k = 3)
        assertEquals(3, results.size)
        // All three mentionTimes are equal, so ordering is pure cosine.
        assertEquals(h1.take(8), results[0].hash)
        assertEquals(h2.take(8), results[1].hash)
        assertEquals(h3.take(8), results[2].hash)
    }

    @Test
    fun `search returns empty when no vectors are indexed`() = runBlocking {
        embedder.queue(listOf(1f, 0f, 0f))
        val results = retrieval.search("query", k = 5)
        assertTrue("empty store must yield empty search", results.isEmpty())
    }

    // ------------------------------------------------------------------
    // search: nodeType filter
    // ------------------------------------------------------------------

    @Test
    fun `search with nodeType filter keeps only matching types`() = runBlocking {
        val eventHash = putEvent("event-content", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        val lcHash = putNode(
            content = "life-capture-content",
            nodeType = "life_capture",
            vector = listOf(1f, 0f, 0f),
            mentionTime = 1_000L,
        )
        embedder.queue(listOf(1f, 0f, 0f))

        val eventsOnly = retrieval.search("query", k = 5, nodeType = "event")
        assertEquals(1, eventsOnly.size)
        assertEquals(eventHash.take(8), eventsOnly[0].hash)

        embedder.queue(listOf(1f, 0f, 0f))
        val lcsOnly = retrieval.search("query", k = 5, nodeType = "life_capture")
        assertEquals(1, lcsOnly.size)
        assertEquals(lcHash.take(8), lcsOnly[0].hash)
    }

    // ------------------------------------------------------------------
    // search: timeRange on mention axis and event axis
    // ------------------------------------------------------------------

    @Test
    fun `search with timeRange on mention axis filters by mention_time point`() = runBlocking {
        // mention axis: the node's mentionTime is treated as a zero-length
        // point interval [t, t). A zero-length interval is empty under the
        // half-open overlap operator, so overlap(qStart, qEnd, t, t) reduces
        // to `qStart < t && t < qEnd` — i.e. the point must be strictly
        // inside the half-open query range (the start boundary is exclusive
        // for a point, matching Python retrieval.py::_in_range).
        // event_time supports minute precision; fixtures align to it.
        // Times under Asia/Shanghai (UTC+8): epoch 480=08:08, 900=08:15, etc.
        putEvent("e-before", vector = listOf(1f, 0f, 0f), mentionTime = 480L)    // 08:08
        putEvent("e-strictly-inside", vector = listOf(1f, 0f, 0f), mentionTime = 1_200L) // 08:20
        putEvent("e-at-start", vector = listOf(1f, 0f, 0f), mentionTime = 900L)  // 08:15
        putEvent("e-future", vector = listOf(1f, 0f, 0f), mentionTime = 9_000L)
        embedder.queue(listOf(1f, 0f, 0f))

        // Query [900, 1800) on mention axis: keeps e-strictly-inside (1200),
        // drops e-at-start (900, excluded because the point interval is
        // empty), e-before (480), and e-future (9000).
        val results = retrieval.search(
            "query", k = 5,
            timeRange = "1970-01-01T08:15/1970-01-01T08:30",
            timeAxis = "mention",
        )
        assertEquals(1, results.size)
        assertTrue(results[0].preview.contains("strictly-inside"))
    }

    @Test
    fun `search with timeRange on event axis filters by start_ts end_ts overlap`() = runBlocking {
        // event axis: nodes have [startTs, endTs) from event_time expansion.
        // Use day-precision event_time so the interval is a full day.
        // "2026-08-08" under Asia/Shanghai → [1786118400, 1786204800).
        putEvent(
            content = "event-day",
            eventTime = "2026-08-08",
            vector = listOf(1f, 0f, 0f),
            mentionTime = 1_000L,
        )
        putEvent(
            content = "event-other-day",
            eventTime = "2026-08-10",
            vector = listOf(1f, 0f, 0f),
            mentionTime = 1_000L,
        )
        embedder.queue(listOf(1f, 0f, 0f))

        // Query the same day → only event-day matches.
        val sameDay = retrieval.search(
            "query", k = 5,
            timeRange = "2026-08-08",
            timeAxis = "event",
        )
        assertEquals(1, sameDay.size)
        assertTrue(sameDay[0].preview.contains("event-day"))

        // Query a range that spans both days → both match.
        embedder.queue(listOf(1f, 0f, 0f))
        val bothDays = retrieval.search(
            "query", k = 5,
            timeRange = "2026-08-08/2026-08-11",
            timeAxis = "event",
        )
        assertEquals(2, bothDays.size)
    }

    @Test
    fun `search with timeRange event axis boundary does not overlap touching intervals`() = runBlocking {
        // Node event_time "2026-08-08" → [1786118400, 1786204800).
        putEvent(
            content = "boundary-event",
            eventTime = "2026-08-08",
            vector = listOf(1f, 0f, 0f),
            mentionTime = 1_000L,
        )
        embedder.queue(listOf(1f, 0f, 0f))

        // Query "2026-08-09" → [1786204800, 1786291200). Touches the node's
        // end exclusively → no overlap (half-open).
        val touching = retrieval.search(
            "query", k = 5,
            timeRange = "2026-08-09",
            timeAxis = "event",
        )
        assertTrue("touching endpoints must not overlap", touching.isEmpty())
    }

    // ------------------------------------------------------------------
    // search: entity filter
    // ------------------------------------------------------------------

    @Test
    fun `search with entity filter restricts to nodes linked to the entity`() = runBlocking {
        val entHash = putEntity("Alice", aliases = listOf("Alicia"))
        val linkedEvent = putEvent("event-with-alice", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        putEvent("event-without-alice", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        linkEventEntity(linkedEvent, entHash, role = "subject")

        // By canonical name. (One queued vector per search call.)
        embedder.queue(listOf(1f, 0f, 0f))
        val byName = retrieval.search("query", k = 5, entity = "Alice")
        assertEquals(1, byName.size)
        assertEquals(linkedEvent.take(8), byName[0].hash)

        // By alias.
        embedder.queue(listOf(1f, 0f, 0f))
        val byAlias = retrieval.search("query", k = 5, entity = "Alicia")
        assertEquals(1, byAlias.size)
        assertEquals(linkedEvent.take(8), byAlias[0].hash)

        // Unresolvable entity → empty list (no throw, no embed call — the
        // entity resolution happens before embedding).
        val unknown = retrieval.search("query", k = 5, entity = "Nobody")
        assertTrue("unresolvable entity must yield empty list", unknown.isEmpty())
    }

    // ------------------------------------------------------------------
    // search: includeSuperseded default-false vs true
    // ------------------------------------------------------------------

    @Test
    fun `search hides superseded nodes by default and includes them when asked`() = runBlocking {
        val h = putEvent("superseded-event", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        // Supersede it.
        val w = freshWriter()
        w.updateNode(h, mapOf("superseded_by" to "replacement-hash"))
        w.flush(store)

        // Default: superseded node is filtered out by getNode, so it never
        // enters the candidate list even though its vector is still indexed.
        embedder.queue(listOf(1f, 0f, 0f))
        val default = retrieval.search("query", k = 5)
        assertTrue("superseded node must be hidden by default", default.isEmpty())

        // includeSuperseded = true: the node is eligible again.
        embedder.queue(listOf(1f, 0f, 0f))
        val included = retrieval.search("query", k = 5, includeSuperseded = true)
        assertEquals(1, included.size)
        assertEquals(h.take(8), included[0].hash)
    }

    // ------------------------------------------------------------------
    // search: k cap enforcement
    // ------------------------------------------------------------------

    @Test
    fun `search truncates to k results`() = runBlocking {
        putEvent("e1", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        putEvent("e2", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        putEvent("e3", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        putEvent("e4", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        embedder.queue(listOf(1f, 0f, 0f))

        val results = retrieval.search("query", k = 2)
        assertEquals("k must cap the result count", 2, results.size)
    }

    @Test
    fun `search with k le 0 returns empty without embedding`() = runBlocking {
        // No vector queued — if the embedder were called, queue() would throw
        // on the next embed. k <= 0 short-circuits before embedding.
        val results = retrieval.search("query", k = 0)
        assertTrue(results.isEmpty())
        val neg = retrieval.search("query", k = -1)
        assertTrue(neg.isEmpty())
        // Confirm the embedder was never polled.
        assertEquals(0, embedder.callCount)
    }

    // ------------------------------------------------------------------
    // search: bad timeAxis
    // ------------------------------------------------------------------

    @Test
    fun `search rejects a bad timeAxis`() = runBlocking {
        var threw = false
        try {
            retrieval.search("query", k = 5, timeAxis = "nope")
        } catch (e: ToolValidationError) {
            threw = true
            assertTrue(e.message!!.contains("time_axis"))
        }
        assertTrue("bad timeAxis must raise ToolValidationError", threw)
    }

    // ------------------------------------------------------------------
    // temporal: mention axis and event axis incl. boundary
    // ------------------------------------------------------------------

    @Test
    fun `temporal on mention axis keeps nodes whose mention_time falls in the half-open range`() = runBlocking {
        // event_time supports minute precision (Thh:mm), not seconds; fixture
        // mention times align to minute boundaries. Times are in Asia/Shanghai
        // (UTC+8): epoch 60 = 08:01, 120 = 08:02, etc.
        putEvent("at-start", mentionTime = 60L)   // 08:01
        putEvent("inside", mentionTime = 120L)    // 08:02
        putEvent("at-end", mentionTime = 180L)    // 08:03
        putEvent("outside", mentionTime = 300L)   // 08:05

        val results = retrieval.temporal(
            "1970-01-01T08:01/1970-01-01T08:02",
            axis = "mention",
            k = 10,
        )
        // [60, 180): keeps at-start (60) and inside (120), drops at-end
        // (180, exclusive) and outside (300).
        assertEquals(2, results.size)
    }

    @Test
    fun `temporal on event axis keeps nodes whose interval overlaps the query`() = runBlocking {
        putEvent("day-8", eventTime = "2026-08-08", mentionTime = 1_000L)
        putEvent("day-10", eventTime = "2026-08-10", mentionTime = 1_000L)

        // Query "2026-08-08/2026-08-09" → overlaps day-8 only.
        val oneDay = retrieval.temporal("2026-08-08/2026-08-09", axis = "event", k = 10)
        assertEquals(1, oneDay.size)
        assertTrue(oneDay[0].preview.contains("day-8"))

        // Query "2026-08-08/2026-08-11" → overlaps both.
        val both = retrieval.temporal("2026-08-08/2026-08-11", axis = "event", k = 10)
        assertEquals(2, both.size)
    }

    @Test
    fun `temporal event axis boundary does not match touching intervals`() = runBlocking {
        putEvent("day-8", eventTime = "2026-08-08", mentionTime = 1_000L)
        // Query "2026-08-09" → [1786204800, 1786291200). Node day-8 is
        // [1786118400, 1786204800). Touching at 1786204800 → no overlap.
        val touching = retrieval.temporal("2026-08-09", axis = "event", k = 10)
        assertTrue("touching endpoints must not overlap", touching.isEmpty())
    }

    @Test
    fun `temporal with nodeType filter`() = runBlocking {
        putEvent("evt", mentionTime = 120L)  // 08:02 Asia/Shanghai
        putNode("lc", nodeType = "life_capture", mentionTime = 120L)
        val events = retrieval.temporal(
            "1970-01-01T08:01/1970-01-01T08:05",
            axis = "mention",
            k = 10,
            nodeType = "event",
        )
        assertEquals(1, events.size)
        assertTrue(events[0].preview.contains("evt"))
    }

    @Test
    fun `temporal rejects a bad axis`() = runBlocking {
        var threw = false
        try {
            retrieval.temporal("2026-08-08", axis = "nope", k = 10)
        } catch (e: ToolValidationError) {
            threw = true
            assertTrue(e.message!!.contains("axis"))
        }
        assertTrue(threw)
    }

    @Test
    fun `temporal with k le 0 returns empty`() = runBlocking {
        putEvent("e", mentionTime = 120L)
        val results = retrieval.temporal("1970-01-01T08:01/1970-01-01T08:05", k = 0)
        assertTrue(results.isEmpty())
    }

    // ------------------------------------------------------------------
    // entityEvents: ordering + limit
    // ------------------------------------------------------------------

    @Test
    fun `entityEvents returns events linked to the entity, mention_time descending`() = runBlocking {
        val entHash = putEntity("Bob")
        val e1 = putEvent("event-1", mentionTime = 100L)
        val e2 = putEvent("event-2", mentionTime = 300L)
        val e3 = putEvent("event-3", mentionTime = 200L)
        linkEventEntity(e1, entHash, "subject")
        linkEventEntity(e2, entHash, "subject")
        linkEventEntity(e3, entHash, "subject")

        val results = retrieval.entityEvents("Bob", k = 10)
        assertEquals(3, results.size)
        // mention_time descending: e2 (300) > e3 (200) > e1 (100).
        assertEquals(e2.take(8), results[0].hash)
        assertEquals(e3.take(8), results[1].hash)
        assertEquals(e1.take(8), results[2].hash)
    }

    @Test
    fun `entityEvents caps at k`() = runBlocking {
        val entHash = putEntity("Carol")
        linkEventEntity(putEvent("e1", mentionTime = 100L), entHash, "subject")
        linkEventEntity(putEvent("e2", mentionTime = 200L), entHash, "subject")
        linkEventEntity(putEvent("e3", mentionTime = 300L), entHash, "subject")

        val results = retrieval.entityEvents("Carol", k = 2)
        assertEquals(2, results.size)
    }

    @Test
    fun `entityEvents resolves by alias`() = runBlocking {
        val entHash = putEntity("Dave", aliases = listOf("David"))
        val e = putEvent("event-dave", mentionTime = 100L)
        linkEventEntity(e, entHash, "subject")

        val results = retrieval.entityEvents("David", k = 10)
        assertEquals(1, results.size)
        assertEquals(e.take(8), results[0].hash)
    }

    @Test
    fun `entityEvents resolves by hash`() = runBlocking {
        val entHash = putEntity("Eve")
        val e = putEvent("event-eve", mentionTime = 100L)
        linkEventEntity(e, entHash, "subject")

        val results = retrieval.entityEvents(entHash, k = 10)
        assertEquals(1, results.size)
    }

    @Test
    fun `entityEvents returns empty for unresolvable entity`() = runBlocking {
        val results = retrieval.entityEvents("Nobody", k = 10)
        assertTrue(results.isEmpty())
    }

    @Test
    fun `entityEvents with k le 0 returns empty`() = runBlocking {
        val entHash = putEntity("Frank")
        linkEventEntity(putEvent("e", mentionTime = 100L), entHash, "subject")
        val results = retrieval.entityEvents("Frank", k = 0)
        assertTrue(results.isEmpty())
    }

    @Test
    fun `entityEvents filters superseded events`() = runBlocking {
        val entHash = putEntity("Grace")
        val live = putEvent("live-event", mentionTime = 100L)
        val superseded = putEvent("old-event", mentionTime = 200L)
        linkEventEntity(live, entHash, "subject")
        linkEventEntity(superseded, entHash, "subject")

        // Supersede the second event.
        val w = freshWriter()
        w.updateNode(superseded, mapOf("superseded_by" to "new-hash"))
        w.flush(store)

        val results = retrieval.entityEvents("Grace", k = 10)
        assertEquals("superseded events must be filtered out", 1, results.size)
        assertEquals(live.take(8), results[0].hash)
    }

    // ------------------------------------------------------------------
    // expand: full content + metadata + links + supersede chain + access bump
    // ------------------------------------------------------------------

    @Test
    fun `expand returns full node content and metadata`() = runBlocking {
        val h = putEvent(
            content = "full-detail-event",
            mentionTime = 1_000L,
            metadata = mapOf("importance_note" to "high"),
        )
        val expanded = retrieval.expand(h)!!
        assertEquals(h, expanded.node.hashId)
        assertEquals("full-detail-event", expanded.node.content)
        assertEquals("high", expanded.node.metadata["importance_note"])
    }

    @Test
    fun `expand returns forward entity links with canonical names`() = runBlocking {
        val entHash = putEntity("Henry", appearanceCount = 3)
        val evtHash = putEvent("event-with-henry", mentionTime = 1_000L)
        linkEventEntity(evtHash, entHash, "subject")

        val expanded = retrieval.expand(evtHash)!!
        assertEquals(1, expanded.entities.size)
        val link = expanded.entities[0]
        assertEquals(entHash, link.hash)
        assertEquals("Henry", link.canonicalName)
        assertEquals("subject", link.role)
        assertEquals(1, link.mentionCount)
    }

    @Test
    fun `expand returns reverse event links for entity nodes`() = runBlocking {
        val entHash = putEntity("Iris")
        val e1 = putEvent("event-1", mentionTime = 100L)
        val e2 = putEvent("event-2", mentionTime = 300L)
        linkEventEntity(e1, entHash, "subject")
        linkEventEntity(e2, entHash, "subject")

        val expanded = retrieval.expand(entHash)!!
        // Reverse links, mention_time descending.
        assertEquals(2, expanded.events.size)
        assertEquals(e2, expanded.events[0].hash)
        assertEquals(e1, expanded.events[1].hash)
        // Preview is the truncated content.
        assertNotNull(expanded.events[0].preview)
        assertEquals(300L, expanded.events[0].mentionTime)
    }

    @Test
    fun `expand bumps access_count via recordAccess`() = runBlocking {
        val h = putEvent("access-target", mentionTime = 1_000L)
        assertEquals(0, store.getNode(h)!!.accessCount)

        retrieval.expand(h)
        assertEquals(1, store.getNode(h)!!.accessCount)
        assertNotNull(store.getNode(h)!!.lastAccessed)

        retrieval.expand(h)
        assertEquals(2, store.getNode(h)!!.accessCount)
    }

    @Test
    fun `expand returns null for missing node without recording access`() = runBlocking {
        val result = retrieval.expand("nonexistent-hash-id")
        assertNull(result)
        // No access recorded (the node doesn't exist, so there's nothing to
        // bump — recordAccess is a silent no-op on a missing hash anyway).
    }

    @Test
    fun `expand follows the supersede chain forward`() = runBlocking {
        val old = putEvent("old-event", mentionTime = 1_000L)
        // Create two successor nodes and chain them: old → mid → new.
        val mid = putEvent("mid-event", mentionTime = 1_000L)
        val newHash = putEvent("new-event", mentionTime = 1_000L)

        val w1 = freshWriter()
        w1.updateNode(old, mapOf("superseded_by" to mid))
        w1.updateNode(mid, mapOf("superseded_by" to newHash))
        w1.flush(store)

        val expanded = retrieval.expand(old)!!
        assertEquals(listOf(mid, newHash), expanded.supersedeChain)
    }

    @Test
    fun `expand is cycle-safe on the supersede chain`() = runBlocking {
        val a = putEvent("cycle-a", mentionTime = 1_000L)
        val b = putEvent("cycle-b", mentionTime = 1_000L)
        val w = freshWriter()
        // Mutually point at each other (a corruption scenario the cycle
        // guard must handle without infinite recursion).
        w.updateNode(a, mapOf("superseded_by" to b))
        w.updateNode(b, mapOf("superseded_by" to a))
        w.flush(store)

        val expanded = retrieval.expand(a)!!
        // The chain stops once it revisits a node already seen.
        assertEquals(listOf(b), expanded.supersedeChain)
    }

    // ------------------------------------------------------------------
    // expand: predecessors (transitive supersede_by walk, nearest first)
    // ------------------------------------------------------------------

    @Test
    fun `expand predecessors walks the supersede chain backward transitively`() = runBlocking {
        // Chain: a superseded_by b, b superseded_by c.
        // expand(c).predecessors must be [b, a] (nearest first, BFS by hashId
        // within each frontier level — mirrors Python retrieval.py::_predecessors).
        val a = putEvent("pred-a", mentionTime = 1_000L)
        val b = putEvent("pred-b", mentionTime = 1_000L)
        val c = putEvent("pred-c", mentionTime = 1_000L)
        val w = freshWriter()
        w.updateNode(a, mapOf("superseded_by" to b))
        w.updateNode(b, mapOf("superseded_by" to c))
        w.flush(store)

        val expanded = retrieval.expand(c)!!
        assertEquals(listOf(b, a), expanded.predecessors)
    }

    @Test
    fun `expand predecessors is empty when nothing points at the node`() = runBlocking {
        val h = putEvent("no-preds", mentionTime = 1_000L)
        val expanded = retrieval.expand(h)!!
        assertTrue("a node with no incoming supersede must have no predecessors",
            expanded.predecessors.isEmpty())
    }

    @Test
    fun `expand predecessors is cycle-safe`() = runBlocking {
        // Mutual supersede (a corruption scenario): a→b and b→a. The BFS
        // visited-set must stop the walk without looping.
        val a = putEvent("cyc-a", mentionTime = 1_000L)
        val b = putEvent("cyc-b", mentionTime = 1_000L)
        val w = freshWriter()
        w.updateNode(a, mapOf("superseded_by" to b))
        w.updateNode(b, mapOf("superseded_by" to a))
        w.flush(store)

        val expanded = retrieval.expand(a)!!
        // expand(a): predecessors start from a; by_successor[a] = [b] (b→a),
        // then by_successor[b] = [a] but a is already visited → stop.
        assertEquals(listOf(b), expanded.predecessors)
    }

    @Test
    fun `expand predecessors resolves ties by hashId for deterministic order`() = runBlocking {
        // Two nodes both superseded by the same successor: the BFS sorts
        // each frontier level by hashId, so the order is deterministic.
        val successor = putEvent("succ", mentionTime = 1_000L)
        val p1 = putEvent("pred-1", mentionTime = 1_000L)
        val p2 = putEvent("pred-2", mentionTime = 1_000L)
        val w = freshWriter()
        w.updateNode(p1, mapOf("superseded_by" to successor))
        w.updateNode(p2, mapOf("superseded_by" to successor))
        w.flush(store)

        val expanded = retrieval.expand(successor)!!
        assertEquals(listOf(p1, p2).sortedBy { it }, expanded.predecessors)
    }

    @Test
    fun `expand on a superseded node still works and records access`() = runBlocking {
        val h = putEvent("superseded-expand-target", mentionTime = 1_000L)
        val w = freshWriter()
        w.updateNode(h, mapOf("superseded_by" to "successor"))
        w.flush(store)

        // Superseded nodes remain expandable.
        val expanded = retrieval.expand(h)
        assertNotNull("superseded nodes must remain expandable", expanded)
        // Access is still recorded.
        assertEquals(1, store.getNode(h, includeSuperseded = true)!!.accessCount)
    }

    @Test
    fun `expand truncates long content previews for reverse event links`() = runBlocking {
        val entHash = putEntity("Jane")
        val longContent = "x".repeat(Operators.PREVIEW_LENGTH + 50)
        val e = putEvent(longContent, mentionTime = 100L)
        linkEventEntity(e, entHash, "subject")

        val expanded = retrieval.expand(entHash)!!
        val preview = expanded.events[0].preview!!
        assertTrue("preview must be truncated", preview.endsWith("[+]"))
        assertEquals(Operators.PREVIEW_LENGTH + 3, preview.length) // content + "[+]"
    }

    // ------------------------------------------------------------------
    // hash-prefix expansion: Python expand does NOT support prefixes
    // ------------------------------------------------------------------

    @Test
    fun `expand takes a full hash, not a prefix - mirrors Python retrieval_py`() = runBlocking {
        val h = putEvent("prefix-test", mentionTime = 1_000L)
        // Full hash works.
        assertNotNull(retrieval.expand(h))
        // A short prefix does NOT resolve (Python retrieval.py::expand takes
        // the full hash_id directly; prefix resolution is the caller's job,
        // done via MemStore.resolveHash before calling expand).
        assertNull(retrieval.expand(h.take(8)))
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    /** Fresh writer with the same dim as the store under test. */
    private fun freshWriter(): TxWriter = TxWriter(dimensions = 3)

    /**
     * Insert an event node with an optional vector, event_time, and metadata.
     *
     * Defaults: `nodeType = "event"`, no event_time, empty metadata. The
     * mentionTime defaults to a small value so the rank decay math is stable
     * under [fixedNow].
     *
     * @return The node's 12-char hash id.
     */
    private suspend fun putEvent(
        content: String,
        eventTime: String? = null,
        vector: List<Float>? = null,
        mentionTime: Long = 1_000L,
        metadata: Map<String, Any?> = emptyMap(),
    ): String = putNode(
        content = content,
        nodeType = "event",
        eventTime = eventTime,
        vector = vector,
        mentionTime = mentionTime,
        metadata = metadata,
    )

    /**
     * Insert an arbitrary node with an optional vector.
     *
     * @return The node's 12-char hash id.
     */
    private suspend fun putNode(
        content: String,
        nodeType: String = "event",
        baseType: String = "content",
        eventTime: String? = null,
        vector: List<Float>? = null,
        mentionTime: Long = 1_000L,
        metadata: Map<String, Any?> = emptyMap(),
    ): String {
        val w = freshWriter()
        val h = w.putNode(
            nodeType = nodeType,
            baseType = baseType,
            content = content,
            eventTime = eventTime,
            mentionTime = mentionTime,
            metadata = metadata,
        )
        if (vector != null) w.putVector(h, vector)
        w.flush(store)
        return h
    }

    /**
     * Insert an entity card with canonical_name, optional aliases, and an
     * optional `appearance_count`.
     *
     * @return The entity node's 12-char hash id.
     */
    private suspend fun putEntity(
        canonicalName: String,
        aliases: List<String> = emptyList(),
        appearanceCount: Int = 0,
    ): String {
        val meta: MutableMap<String, Any?> = LinkedHashMap()
        meta["canonical_name"] = canonicalName
        if (aliases.isNotEmpty()) meta["aliases"] = aliases
        if (appearanceCount > 0) meta["appearance_count"] = appearanceCount
        return putNode(
            content = canonicalName,
            nodeType = "entity",
            baseType = "entity",
            metadata = meta,
            mentionTime = 1_000L,
        )
    }

    /** Link an event node to an entity node with a role. */
    private suspend fun linkEventEntity(eventHash: String, entityHash: String, role: String) {
        val w = freshWriter()
        w.putLink(eventHash, entityHash, role)
        w.flush(store)
    }
}

/**
 * Hand-written fake [EmbeddingClient] for retrieval tests.
 *
 * Returns pre-queued deterministic vectors in FIFO order. Each call to [embed]
 * pops one queued vector; if the queue is empty when [embed] is called, the
 * test fails fast with an assertion (the test did not queue enough vectors for
 * the search calls it made).
 *
 * No mock framework — just a minimal hand-written implementation, per the
 * task's "hand-written fakes only" constraint.
 */
private class FakeEmbedder(override val dimension: Int) : EmbeddingClient {
    private val queue: ArrayDeque<FloatArray> = ArrayDeque()
    var callCount: Int = 0
        private set

    /** Queue one vector to be returned by the next [embed] call. */
    fun queue(vectors: List<Float>) {
        queue.addLast(vectors.toFloatArray())
    }

    override suspend fun embed(texts: List<String>): List<FloatArray> {
        callCount += 1
        if (queue.isEmpty()) {
            throw IllegalStateException(
                "FakeEmbedder: no vector queued for embed call " +
                    "(texts=${texts.size}, first='${texts.firstOrNull()?.take(20)}')"
            )
        }
        val vec = queue.removeFirst()
        check(vec.size == dimension) {
            "FakeEmbedder: queued vector dim ${vec.size} != expected $dimension"
        }
        return listOf(vec)
    }
}

/**
 * Throwaway test-only [RoomDatabase] that registers ONLY the four mem entities.
 *
 * Same pattern as `MemStoreTest.MemStoreTestDb`; declared separately here so
 * this test class is self-contained and does not collide with another test
 * file's private database.
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
abstract class MemRetrievalTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
