package com.l2dchat.core.mem

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.test.core.app.ApplicationProvider
import com.google.gson.Gson
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmFinishReason
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmTokenUsage
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import java.time.ZoneId
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
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
 * Tests for [buildLtmTools] — the host planner tool surface (plan task 6a).
 *
 * Mirrors the behavior of `mem/planner.py::build_planner_tools`. Runs against a
 * throwaway test-only [LtmToolsTestDb] (same pattern as [MemRetrievalTest]'s
 * `MemRetrievalTestDb`) so the SQL semantics under test — prefix resolution,
 * node reads, link joins — are exercised against a real SQLite engine under
 * Robolectric.
 *
 * The fixture is built via [TxWriter] (the production write path) with a
 * hand-written [FakeEmbedder] that returns deterministic vectors, so the
 * cosine ranking is fully reproducible. The `ltm_store` happy path uses a
 * [ScriptedLlmClient] (same pattern as [ExtractionPipelineTest]) to drive the
 * extraction pipeline without a real provider.
 *
 * Coverage (per task 6a MUST-DO list):
 * - `ltm_search` with query (semantic path).
 * - `ltm_search` time-range-only (temporal path).
 * - `ltm_search` neither query nor time_range → error payload (no throw).
 * - `k` cap enforcement (k > 20 → error payload).
 * - `ltm_expand` full hash.
 * - `ltm_expand` prefix resolution (≥6-char prefix).
 * - `ltm_expand` ambiguous prefix → error payload with candidate list.
 * - `ltm_expand` unknown prefix → error payload.
 * - `ltm_read_media` on a life_capture node with media_path → returns
 *   `{media_path, modality, captured_at, error}`.
 * - `ltm_read_media` on a node without media_path → error payload.
 * - `ltm_read_media` does NOT bump `access_count`.
 * - `ltm_read_media` prefix resolution.
 * - `ltm_store` absent when `pipeline = null` or `enableStore = false`.
 * - `ltm_store` happy path: batch_id format `manual_<hash>_<YYYYMMDD>`,
 *   `source = "manual"`, nodes persisted.
 * - `ltm_store` idempotent: same text same day returns the existing receipt
 *   without a second LLM invocation.
 * - All tools never throw on bad input (garbage args → error payload).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class LtmToolsTest {

    private lateinit var db: LtmToolsTestDb
    private lateinit var store: MemStore
    private lateinit var embedder: LtmFakeEmbedder
    private lateinit var pipelineEmbedder: LtmDeterministicEmbedder
    private lateinit var retrieval: MemRetrieval
    private lateinit var registry: NodeTypeRegistry
    private lateinit var llmConfig: LlmGenerationConfig

    /** Fixed timezone so time-range parsing and the YYYYMMDD suffix are deterministic. */
    private val zone: ZoneId = ZoneId.of("Asia/Shanghai")

    /** Fixed `now` for the rank decay math and the `ltm_store` idempotency key. */
    private val fixedNow: Long = 1_786_116_600L // 2026-08-07 23:30 UTC+8 (Asia/Shanghai)

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            LtmToolsTestDb::class.java,
        ).allowMainThreadQueries().build()
        store = MemStore(
            nodeDao = db.memNodeDao(),
            linkDao = db.memLinkDao(),
            vectorDao = db.memVectorDao(),
            receiptDao = db.memReceiptDao(),
            expectedDim = 3,
            transactionRunner = null,
            zoneId = zone,
        )
        runBlocking { store.load() }
        embedder = LtmFakeEmbedder(dimension = 3)
        pipelineEmbedder = LtmDeterministicEmbedder(dim = 3)
        retrieval = MemRetrieval(
            store = store,
            embedder = embedder,
            zoneId = zone,
            nowFn = { fixedNow },
        )
        registry = NodeTypeRegistry()
        llmConfig = LlmGenerationConfig(model = "test-model")
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // ltm_search: semantic path
    // ------------------------------------------------------------------

    @Test
    fun `ltm_search with query returns summaries best first`() = runBlocking {
        val h1 = putEvent("event-a", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        putEvent("event-b", vector = listOf(0f, 1f, 0f), mentionTime = 1_000L)
        embedder.queue(listOf(1f, 0f, 0f))

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(mapOf("query" to "a", "k" to 1))

        assertNull(result["error"])
        @Suppress("UNCHECKED_CAST")
        val summaries = result["results"] as List<Map<String, Any?>>
        assertEquals(1, summaries.size)
        assertEquals(h1.take(8), summaries[0]["hash"])
    }

    @Test
    fun `ltm_search with query applies node_type filter`() = runBlocking {
        putEvent("event-content", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L)
        val lc = putNode(
            content = "lc-content",
            nodeType = "life_capture",
            vector = listOf(1f, 0f, 0f),
            mentionTime = 1_000L,
        )
        embedder.queue(listOf(1f, 0f, 0f))

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(
            mapOf("query" to "x", "k" to 5, "node_type" to "life_capture"),
        )

        assertNull(result["error"])
        @Suppress("UNCHECKED_CAST")
        val summaries = result["results"] as List<Map<String, Any?>>
        assertEquals(1, summaries.size)
        assertEquals(lc.take(8), summaries[0]["hash"])
    }

    // ------------------------------------------------------------------
    // ltm_search: temporal path
    // ------------------------------------------------------------------

    @Test
    fun `ltm_search without query but with time_range runs a temporal scan`() = runBlocking {
        // mention axis: epoch 120 = 08:02 Asia/Shanghai, inside [08:01, 08:04).
        // The A/B interval is closed, so B=08:03 (minute precision) expands to
        // the end of 08:03:00 = 08:04:00 (epoch 240). "outside" at epoch 300
        // (08:05) is excluded.
        putEvent("inside", mentionTime = 120L)
        putEvent("outside", mentionTime = 300L)

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(
            mapOf(
                "time_range" to "1970-01-01T08:01/1970-01-01T08:03",
                "time_axis" to "mention",
                "k" to 5,
            ),
        )

        assertNull(result["error"])
        @Suppress("UNCHECKED_CAST")
        val summaries = result["results"] as List<Map<String, Any?>>
        assertEquals(1, summaries.size)
        assertTrue((summaries[0]["preview"] as String).contains("inside"))
    }

    // ------------------------------------------------------------------
    // ltm_search: neither query nor time_range → error payload
    // ------------------------------------------------------------------

    @Test
    fun `ltm_search with neither query nor time_range returns an error payload without throwing`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(mapOf("k" to 5))

        @Suppress("UNCHECKED_CAST")
        val results = result["results"] as List<*>
        assertTrue("error payload must carry an empty results list", results.isEmpty())
        val error = result["error"] as String
        assertTrue(error.contains("provide query or time_range"))
    }

    // ------------------------------------------------------------------
    // ltm_search: k cap enforcement
    // ------------------------------------------------------------------

    @Test
    fun `ltm_search rejects k above 20 with an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(
            mapOf("query" to "x", "k" to 21),
        )

        val error = result["error"] as String
        assertTrue("k=21 must be rejected: $error", error.contains("k:"))
        assertTrue(error.contains("[1, 20]"))
    }

    @Test
    fun `ltm_search rejects non-integer k with an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(
            mapOf("query" to "x", "k" to "five"),
        )
        val error = result["error"] as String
        assertTrue(error.contains("k:"))
    }

    @Test
    fun `ltm_search accepts default k of 5 when k is omitted`() = runBlocking {
        // Seed 6 candidates; default k=5 must cap the result list.
        repeat(6) { i ->
            putEvent("event-$i", vector = listOf(1f, 0f, 0f), mentionTime = 1_000L + i)
        }
        embedder.queue(listOf(1f, 0f, 0f))

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(mapOf("query" to "x"))

        assertNull(result["error"])
        @Suppress("UNCHECKED_CAST")
        val summaries = result["results"] as List<*>
        assertEquals("default k=5 must cap results", 5, summaries.size)
    }

    // ------------------------------------------------------------------
    // ltm_expand: full hash
    // ------------------------------------------------------------------

    @Test
    fun `ltm_expand with a full hash returns the full node payload`() = runBlocking {
        val h = putEvent("full-detail-event", mentionTime = 1_000L)

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(mapOf("hash" to h))

        assertNull(result["error"])
        val node = result["node"] as Map<*, *>
        val nodeRow = node["node"] as Map<*, *>
        assertEquals(h, nodeRow["hash_id"])
        assertEquals("full-detail-event", nodeRow["content"])
    }

    // ------------------------------------------------------------------
    // ltm_expand: prefix resolution
    // ------------------------------------------------------------------

    @Test
    fun `ltm_expand with a 6-char prefix resolves to the full hash`() = runBlocking {
        val h = putEvent("prefix-resolve-event", mentionTime = 1_000L)

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(mapOf("hash" to h.take(6)))

        assertNull(result["error"])
        val node = result["node"] as Map<*, *>
        val nodeRow = node["node"] as Map<*, *>
        assertEquals(h, nodeRow["hash_id"])
    }

    // ------------------------------------------------------------------
    // ltm_expand: ambiguous prefix → error + candidates
    // ------------------------------------------------------------------

    @Test
    fun `ltm_expand with an ambiguous prefix returns an error with candidate list`() = runBlocking {
        // SHA-256 6-char-prefix collisions are rare (birthday bound ~16M for
        // 6 base64 chars). Probe a bounded set of nodes; if no collision is
        // found, skip via JUnit Assume so the suite reports the right thing.
        val prefixMap: MutableMap<String, MutableList<String>> = LinkedHashMap()
        var collisionPrefix: String? = null
        var collisionHashes: List<String> = emptyList()
        for (i in 0 until 10_000) {
            val h = putEvent("collision-probe-$i", mentionTime = 1_000L)
            val prefix = h.take(6)
            val bucket = prefixMap.getOrPut(prefix) { ArrayList() }
            bucket.add(h)
            if (bucket.size >= 2 && collisionPrefix == null) {
                collisionPrefix = prefix
                collisionHashes = bucket.toList()
                break
            }
        }
        org.junit.Assume.assumeTrue(
            "no 6-char prefix collision found in 10k probes — skip ambiguous-prefix test",
            collisionPrefix != null,
        )

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(mapOf("hash" to collisionPrefix!!))

        assertNull(result["node"])
        val error = result["error"] as String
        assertTrue("error must mention ambiguity: $error", error.contains("ambiguous"))
        assertTrue(error.contains(collisionPrefix!!))
        // The candidate list is embedded in the error string (first 5).
        for (h in collisionHashes) {
            assertTrue("error must list candidate $h: $error", error.contains(h))
        }
    }

    // ------------------------------------------------------------------
    // ltm_expand: unknown prefix → error payload
    // ------------------------------------------------------------------

    @Test
    fun `ltm_expand with an unknown prefix returns an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(mapOf("hash" to "ZZZZZZZZZZ"))

        assertNull(result["node"])
        val error = result["error"] as String
        assertTrue("error must mention no match: $error", error.contains("no node matches"))
    }

    @Test
    fun `ltm_expand rejects a prefix shorter than 6 chars`() = runBlocking {
        val h = putEvent("short-prefix-event", mentionTime = 1_000L)

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(mapOf("hash" to h.take(5)))

        assertNull(result["node"])
        val error = result["error"] as String
        assertTrue(error.contains("≥6-char prefix"))
    }

    // ------------------------------------------------------------------
    // ltm_read_media: life_capture with media_path
    // ------------------------------------------------------------------

    @Test
    fun `ltm_read_media on a life_capture node returns the media reference fields`() = runBlocking {
        val h = putNode(
            content = "a photo of the park",
            nodeType = "life_capture",
            mentionTime = 1_000L,
            metadata = mapOf(
                "media_path" to "/tmp/park.jpg",
                "modality" to "photo",
                "captured_at" to "2026-08-08T15:00",
            ),
        )

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_read_media").handler(mapOf("hash" to h))

        assertNull(result["error"])
        assertEquals("/tmp/park.jpg", result["media_path"])
        assertEquals("photo", result["modality"])
        assertEquals("2026-08-08T15:00", result["captured_at"])
    }

    // ------------------------------------------------------------------
    // ltm_read_media: no media_path → error payload
    // ------------------------------------------------------------------

    @Test
    fun `ltm_read_media on a node without media_path returns an error payload`() = runBlocking {
        val h = putEvent("plain-event-no-media", mentionTime = 1_000L)

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_read_media").handler(mapOf("hash" to h))

        assertNull(result["media_path"])
        val error = result["error"] as String
        assertTrue("error must mention no media_path: $error", error.contains("no media_path"))
        assertTrue(error.contains(h.take(8)))
    }

    // ------------------------------------------------------------------
    // ltm_read_media: does NOT bump access_count
    // ------------------------------------------------------------------

    @Test
    fun `ltm_read_media does not bump access_count`() = runBlocking {
        val h = putNode(
            content = "a video clip",
            nodeType = "life_capture",
            mentionTime = 1_000L,
            metadata = mapOf(
                "media_path" to "/tmp/clip.mp4",
                "modality" to "video",
                "captured_at" to "2026-08-08T16:00",
            ),
        )
        assertEquals(0, store.getNode(h)!!.accessCount)

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        tools.getValue("ltm_read_media").handler(mapOf("hash" to h))
        tools.getValue("ltm_read_media").handler(mapOf("hash" to h))

        assertEquals(
            "ltm_read_media must not record access",
            0,
            store.getNode(h)!!.accessCount,
        )
        assertNull(
            "ltm_read_media must not stamp last_accessed",
            store.getNode(h)!!.lastAccessed,
        )
    }

    // ------------------------------------------------------------------
    // ltm_read_media: prefix resolution
    // ------------------------------------------------------------------

    @Test
    fun `ltm_read_media resolves a 6-char prefix to the full hash`() = runBlocking {
        val h = putNode(
            content = "an audio note",
            nodeType = "life_capture",
            mentionTime = 1_000L,
            metadata = mapOf(
                "media_path" to "/tmp/note.wav",
                "modality" to "voice",
                "captured_at" to "2026-08-08T17:00",
            ),
        )

        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_read_media").handler(mapOf("hash" to h.take(6)))

        assertNull(result["error"])
        assertEquals("/tmp/note.wav", result["media_path"])
    }

    // ------------------------------------------------------------------
    // ltm_store: absent when pipeline null or enableStore false
    // ------------------------------------------------------------------

    @Test
    fun `ltm_store is absent when pipeline is null`() {
        val tools = buildLtmTools(store, retrieval, pipeline = null, enableStore = true)
        assertFalse("ltm_store must not appear when pipeline is null", "ltm_store" in tools)
        // The three resident tools are still present.
        assertTrue("ltm_search" in tools)
        assertTrue("ltm_expand" in tools)
        assertTrue("ltm_read_media" in tools)
    }

    @Test
    fun `ltm_store is absent when enableStore is false`() {
        val pipeline = makeMinimalPipeline()
        val tools = buildLtmTools(store, retrieval, pipeline = pipeline, enableStore = false)
        assertFalse("ltm_store must not appear when enableStore is false", "ltm_store" in tools)
    }

    @Test
    fun `ltm_store is present when pipeline is non-null and enableStore is true`() {
        val pipeline = makeMinimalPipeline()
        val tools = buildLtmTools(store, retrieval, pipeline = pipeline, enableStore = true)
        assertTrue("ltm_store must appear when both prerequisites are met", "ltm_store" in tools)
    }

    // ------------------------------------------------------------------
    // ltm_ask: absent when searchAgent null, present when provided
    // ------------------------------------------------------------------

    @Test
    fun `ltm_ask is absent when searchAgent is null`() {
        val tools = buildLtmTools(store, retrieval, searchAgent = null)
        assertFalse("ltm_ask must not appear when searchAgent is null", "ltm_ask" in tools)
        // The three resident tools are still present.
        assertTrue("ltm_search" in tools)
        assertTrue("ltm_expand" in tools)
        assertTrue("ltm_read_media" in tools)
    }

    @Test
    fun `ltm_ask is present when searchAgent is non-null`() {
        val agent = makeMinimalSearchAgent()
        val tools = buildLtmTools(store, retrieval, searchAgent = agent)
        assertTrue("ltm_ask must appear when searchAgent is non-null", "ltm_ask" in tools)
    }

    @Test
    fun `ltm_ask happy path delegates to the search agent and returns its payload`() = runBlocking {
        val h = putEvent("delegated-event", vector = listOf(1f, 0f, 0f))
        embedder.queue(listOf(1f, 0f, 0f))
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = LtmScriptedLlmClient(
                responses = listOf(
                    finalTextResponse("answer with citation [${h.take(8)}]"),
                ),
            ),
            config = llmConfig,
            zoneId = zone,
            nowFn = { fixedNow },
        )
        val tools = buildLtmTools(store, retrieval, searchAgent = agent)

        val result = tools.getValue("ltm_ask").handler(mapOf("question" to "what happened?"))

        assertNull(result["error"])
        assertTrue(
            "answer must carry the hash citation: '${result["answer"]}'",
            (result["answer"] as String).contains(h.take(8)),
        )
        @Suppress("UNCHECKED_CAST")
        val stats = result["stats"] as Map<String, Any>
        assertTrue("stats must carry turns", stats.containsKey("turns"))
        assertTrue("stats must carry tool_calls", stats.containsKey("tool_calls"))
        assertTrue("stats must carry usage", stats.containsKey("usage"))
    }

    @Test
    fun `ltm_ask with a blank question returns an error payload without throwing`() = runBlocking {
        val agent = makeMinimalSearchAgent()
        val tools = buildLtmTools(store, retrieval, searchAgent = agent)

        val result = tools.getValue("ltm_ask").handler(mapOf("question" to "   "))

        assertEquals("", result["answer"])
        val error = result["error"] as String
        assertTrue("error must mention the blank question: $error", error.contains("question:"))
    }

    @Test
    fun `ltm_ask with unexpected keys returns an error payload`() = runBlocking {
        val agent = makeMinimalSearchAgent()
        val tools = buildLtmTools(store, retrieval, searchAgent = agent)

        val result = tools.getValue("ltm_ask").handler(
            mapOf("question" to "x", "bogus" to 1),
        )

        assertEquals("", result["answer"])
        assertTrue((result["error"] as String).contains("unexpected arguments"))
    }

    // ------------------------------------------------------------------
    // ltm_store: happy path
    // ------------------------------------------------------------------

    @Test
    fun `ltm_store happy path persists nodes and returns a manual receipt`() = runBlocking {
        val llm = LtmScriptedLlmClient(
            responses = listOf(
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "register_entity",
                        argumentsJson = """{"name":"Alice","entity_type":"person","description":"a friend"}""",
                    ),
                ),
                toolCallResponse(
                    LlmToolCall(
                        id = "c2",
                        name = "register_content",
                        argumentsJson = """{"node_type":"event","content":"Alice visited","entity_mentions":["Alice"],"event_time":"2026-08-08"}""",
                    ),
                ),
                resolverToolCallResponse(
                    toolName = "link_event_entity",
                    toolCallId = "c3",
                    argBuilder = { prior ->
                        mapOf(
                            "content_hash" to prior[1]["hash"] as String,
                            "entity_hash" to prior[0]["hash"] as String,
                            "role" to "主角",
                        )
                    },
                ),
                finalTextResponse("Stored."),
            ),
        )
        val pipeline = ExtractionPipeline(
            store = store,
            llm = llm,
            config = llmConfig,
            embedder = pipelineEmbedder,
            registry = registry,
            zoneId = zone,
        )
        val text = "Alice visited yesterday"
        val expectedBatchId = "manual_${Hashing.shortHash(text)}_${yyyymmdd(fixedNow, zone)}"

        val tools = buildLtmTools(
            store, retrieval,
            pipeline = pipeline, enableStore = true,
            nowFn = { fixedNow }, zoneId = zone,
        )
        val result = tools.getValue("ltm_store").handler(mapOf("text" to text))

        assertEquals(expectedBatchId, result["batch_id"])
        assertEquals("done", result["status"])
        assertEquals(null, result["error"])
        assertEquals(2, result["node_count"]) // entity + content
        assertEquals(1, result["link_count"])

        // The receipt is persisted with source = "manual".
        val stored = store.getReceipt(expectedBatchId)
        assertNotNull(stored)
        assertEquals(ReceiptStatus.DONE, stored!!.status)
        assertEquals("manual", stored.source)
        // The nodes are in the store.
        for (h in stored.nodeHashes) {
            assertNotNull(store.getNode(h))
        }
    }

    // ------------------------------------------------------------------
    // ltm_store: idempotent same-text same-day
    // ------------------------------------------------------------------

    @Test
    fun `ltm_store idempotent same-text same-day returns the existing receipt without a second LLM call`() = runBlocking {
        val llm = LtmScriptedLlmClient(
            responses = listOf(
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "register_entity",
                        argumentsJson = """{"name":"Bob","entity_type":"person","description":"x"}""",
                    ),
                ),
                toolCallResponse(
                    LlmToolCall(
                        id = "c2",
                        name = "register_content",
                        argumentsJson = """{"node_type":"event","content":"Bob did something","entity_mentions":["Bob"]}""",
                    ),
                ),
                resolverToolCallResponse(
                    toolName = "link_event_entity",
                    toolCallId = "c3",
                    argBuilder = { prior ->
                        mapOf(
                            "content_hash" to prior[1]["hash"] as String,
                            "entity_hash" to prior[0]["hash"] as String,
                        )
                    },
                ),
                finalTextResponse("done"),
            ),
        )
        val pipeline = ExtractionPipeline(
            store = store, llm = llm, config = llmConfig,
            embedder = pipelineEmbedder, registry = registry, zoneId = zone,
        )
        val text = "Bob did something"
        val tools = buildLtmTools(
            store, retrieval,
            pipeline = pipeline, enableStore = true,
            nowFn = { fixedNow }, zoneId = zone,
        )

        val first = tools.getValue("ltm_store").handler(mapOf("text" to text))
        assertEquals("done", first["status"])
        val callsAfterFirst = llm.callCount

        // Second call: same text, same day → same batch_id → the pipeline's
        // done-receipt short-circuit fires; the LLM is NOT invoked again.
        val second = tools.getValue("ltm_store").handler(mapOf("text" to text))
        assertEquals("done", second["status"])
        assertEquals(first["batch_id"], second["batch_id"])
        assertEquals(first["node_count"], second["node_count"])
        assertEquals(callsAfterFirst, llm.callCount)
    }

    // ------------------------------------------------------------------
    // ltm_store: blank text → error payload
    // ------------------------------------------------------------------

    @Test
    fun `ltm_store with blank text returns an error payload without throwing`() = runBlocking {
        val pipeline = makeMinimalPipeline()
        val tools = buildLtmTools(
            store, retrieval,
            pipeline = pipeline, enableStore = true,
            nowFn = { fixedNow }, zoneId = zone,
        )
        val result = tools.getValue("ltm_store").handler(mapOf("text" to "   "))

        assertNull(result["batch_id"])
        assertNull(result["status"])
        val error = result["error"] as String
        assertTrue(error.contains("text:"))
    }

    // ------------------------------------------------------------------
    // All tools: garbage args → error payload, never throw
    // ------------------------------------------------------------------

    @Test
    fun `ltm_search with unexpected keys returns an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_search").handler(
            mapOf("query" to "x", "bogus" to 1, "another" to 2),
        )
        val error = result["error"] as String
        assertTrue(error.contains("unexpected arguments"))
        assertTrue(error.contains("another"))
        assertTrue(error.contains("bogus"))
    }

    @Test
    fun `ltm_expand with a non-string hash returns an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(mapOf("hash" to 12345))
        assertNull(result["node"])
        val error = result["error"] as String
        assertTrue(error.contains("hash:"))
    }

    @Test
    fun `ltm_expand with a null hash returns an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_expand").handler(emptyMap())
        assertNull(result["node"])
        assertTrue((result["error"] as String).contains("hash:"))
    }

    @Test
    fun `ltm_read_media with a missing hash returns an error payload`() = runBlocking {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val result = tools.getValue("ltm_read_media").handler(emptyMap())
        assertNull(result["media_path"])
        assertTrue((result["error"] as String).contains("hash:"))
    }

    @Test
    fun `ltm_store with unexpected keys returns an error payload`() = runBlocking {
        val pipeline = makeMinimalPipeline()
        val tools = buildLtmTools(
            store, retrieval,
            pipeline = pipeline, enableStore = true,
            nowFn = { fixedNow }, zoneId = zone,
        )
        val result = tools.getValue("ltm_store").handler(
            mapOf("text" to "x", "extra" to "y"),
        )
        assertNull(result["batch_id"])
        assertTrue((result["error"] as String).contains("unexpected arguments"))
    }

    @Test
    fun `every tool handler survives a totally garbage arguments map`() = runBlocking {
        val pipeline = makeMinimalPipeline()
        val tools = buildLtmTools(
            store, retrieval,
            pipeline = pipeline, enableStore = true,
            nowFn = { fixedNow }, zoneId = zone,
        )
        val garbage = mapOf("????" to listOf(1, 2, 3), "null" to null)
        // None of these should throw.
        val searchResult = tools.getValue("ltm_search").handler(garbage)
        assertNotNull(searchResult["error"])
        val expandResult = tools.getValue("ltm_expand").handler(garbage)
        assertNotNull(expandResult["error"])
        val mediaResult = tools.getValue("ltm_read_media").handler(garbage)
        assertNotNull(mediaResult["error"])
        val storeResult = tools.getValue("ltm_store").handler(garbage)
        assertNotNull(storeResult["error"])
    }

    // ------------------------------------------------------------------
    // Schema sanity: the OpenAI function-tool schemas are well-formed
    // ------------------------------------------------------------------

    @Test
    fun `every resident tool carries a name, description, and a parameters schema`() {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        for (name in listOf("ltm_search", "ltm_expand", "ltm_read_media")) {
            val tool = tools.getValue(name)
            assertEquals(name, tool.name)
            assertTrue("description must be non-empty", tool.description.isNotEmpty())
            val schema = tool.parametersJsonSchema
            assertEquals("object", schema["type"])
            assertNotNull(schema["properties"])
            assertEquals(false, schema["additionalProperties"])
        }
    }

    @Test
    fun `ltm_search parameters schema matches the planner_py shape`() {
        val tools = buildLtmTools(store, retrieval, nowFn = { fixedNow }, zoneId = zone)
        val schema = tools.getValue("ltm_search").parametersJsonSchema
        @Suppress("UNCHECKED_CAST")
        val properties = schema["properties"] as Map<String, Any?>
        assertTrue("query" in properties)
        assertTrue("k" in properties)
        assertTrue("time_range" in properties)
        assertTrue("time_axis" in properties)
        assertTrue("entity" in properties)
        assertTrue("node_type" in properties)
        @Suppress("UNCHECKED_CAST")
        val timeAxis = properties.getValue("time_axis") as Map<String, Any?>
        assertEquals(listOf("event", "mention"), timeAxis["enum"])
        // required is empty (all params optional).
        assertEquals(emptyList<String>(), schema["required"])
    }

    @Test
    fun `ltm_expand and ltm_read_media require hash and ltm_store requires text`() {
        val pipeline = makeMinimalPipeline()
        val tools = buildLtmTools(
            store, retrieval,
            pipeline = pipeline, enableStore = true,
            nowFn = { fixedNow }, zoneId = zone,
        )
        assertEquals(listOf("hash"), tools.getValue("ltm_expand").parametersJsonSchema["required"])
        assertEquals(listOf("hash"), tools.getValue("ltm_read_media").parametersJsonSchema["required"])
        assertEquals(listOf("text"), tools.getValue("ltm_store").parametersJsonSchema["required"])
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /** Build a minimal pipeline instance for the "tool absent" tests (LLM is never called). */
    private fun makeMinimalPipeline(): ExtractionPipeline = ExtractionPipeline(
        store = store,
        llm = LtmUnsupportedLlmClient(),
        config = llmConfig,
        embedder = pipelineEmbedder,
        registry = registry,
        zoneId = zone,
    )

    /** Build a minimal [MemSearchAgent] for the "tool absent" tests (LLM is never called). */
    private fun makeMinimalSearchAgent(): MemSearchAgent = MemSearchAgent(
        store = store,
        embedder = embedder,
        llm = LtmUnsupportedLlmClient(),
        config = llmConfig,
        zoneId = zone,
        nowFn = { fixedNow },
    )

    /** Format [epoch] seconds as `YYYYMMDD` in [zone], matching the ltm_store suffix. */
    private fun yyyymmdd(epoch: Long, zone: ZoneId): String {
        val formatter = java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd")
        return java.time.Instant.ofEpochSecond(epoch).atZone(zone).toLocalDate().format(formatter)
    }

    /** Fresh writer with the same dim as the store under test. */
    private fun freshWriter(): TxWriter = TxWriter(dimensions = 3)

    /** Insert an event node with an optional vector and event_time. */
    private suspend fun putEvent(
        content: String,
        eventTime: String? = null,
        vector: List<Float>? = null,
        mentionTime: Long = 1_000L,
    ): String = putNode(
        content = content,
        nodeType = "event",
        eventTime = eventTime,
        vector = vector,
        mentionTime = mentionTime,
    )

    /** Insert an arbitrary node with an optional vector. */
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

    /** Build an [LlmResponse] carrying one tool call. */
    private fun toolCallResponse(vararg calls: LlmToolCall): LlmResponse =
        LlmResponse(
            message = LlmMessage.assistantToolCalls(calls.toList()),
            model = "test-model",
            usage = LlmTokenUsage(inputTokens = 10, outputTokens = 5),
            finishReason = LlmFinishReason.TOOL_CALLS,
        )

    /** Build a final-text [LlmResponse] that ends the loop. */
    private fun finalTextResponse(text: String): LlmResponse =
        LlmResponse(
            message = LlmMessage.assistant(text),
            model = "test-model",
            usage = LlmTokenUsage(inputTokens = 10, outputTokens = 5),
            finishReason = LlmFinishReason.STOP,
        )

    /** Build a resolver [LlmResponse] entry for the scripted client. */
    private fun resolverToolCallResponse(
        toolName: String,
        toolCallId: String,
        argBuilder: (priorResults: List<Map<String, Any?>>) -> Map<String, Any?>,
    ): LtmResolverEntry = LtmResolverEntry(toolName, toolCallId, argBuilder)
}

/* ---------------------------------------------------------------------- */
/* Test-only fakes (file-local)                                           */
/* ---------------------------------------------------------------------- */

/**
 * Deterministic embedder for the `ltm_store` pipeline tests.
 *
 * Derives a fixed-dimension vector from the text's hash code so no queueing is
 * needed — the extraction pipeline calls `embed` an unpredictable number of
 * times (once per `register_entity` / `register_content`), and a queued fake
 * would need to know the count ahead of time. Same pattern as
 * [ExtractionPipelineTest]'s `PipelineDeterministicEmbedder`.
 */
private class LtmDeterministicEmbedder(private val dim: Int) : EmbeddingClient {
    override val dimension: Int = dim

    override suspend fun embed(texts: List<String>): List<FloatArray> =
        texts.map { text ->
            val base = text.hashCode()
            FloatArray(dim) { i -> ((base shr (i * 3)) and 0xFF).toFloat() / 255f }
        }
}

/**
 * Hand-written fake [EmbeddingClient] for ltm_tools tests.
 *
 * Returns pre-queued deterministic vectors in FIFO order. Each call to [embed]
 * pops one queued vector; if the queue is empty when [embed] is called, the
 * test fails fast with an assertion.
 */
private class LtmFakeEmbedder(override val dimension: Int) : EmbeddingClient {
    private val queue: ArrayDeque<FloatArray> = ArrayDeque()
    var callCount: Int = 0
        private set

    fun queue(vectors: List<Float>) {
        queue.addLast(vectors.toFloatArray())
    }

    override suspend fun embed(texts: List<String>): List<FloatArray> {
        callCount += 1
        if (queue.isEmpty()) {
            throw IllegalStateException(
                "LtmFakeEmbedder: no vector queued for embed call " +
                    "(texts=${texts.size}, first='${texts.firstOrNull()?.take(20)}')"
            )
        }
        val vec = queue.removeFirst()
        check(vec.size == dimension) {
            "LtmFakeEmbedder: queued vector dim ${vec.size} != expected $dimension"
        }
        return listOf(vec)
    }
}

/**
 * Scripted [LlmClient] fake that returns canned [LlmResponse]s in sequence.
 *
 * Same pattern as [ExtractionPipelineTest]'s `ScriptedLlmClient`, declared
 * file-local here so this test class is self-contained.
 */
private class LtmScriptedLlmClient(
    private val responses: List<Any>,
) : LlmClient {
    private val queue: ArrayDeque<Any> = ArrayDeque(responses)
    var callCount: Int = 0
        private set

    override suspend fun chatCompletion(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): LlmResponse {
        callCount += 1
        if (queue.isEmpty()) {
            throw IllegalStateException("LtmScriptedLlmClient: no more scripted responses")
        }
        val entry = queue.removeFirst()
        return when (entry) {
            is LlmResponse -> entry
            is LtmResolverEntry -> {
                val priorResults = extractPriorToolResults(messages)
                val args = entry.argBuilder(priorResults)
                val argsJson = Gson().toJson(args)
                LlmResponse(
                    message = LlmMessage.assistantToolCalls(
                        listOf(LlmToolCall(id = entry.toolCallId, name = entry.toolName, argumentsJson = argsJson)),
                    ),
                    model = "test-model",
                    usage = LlmTokenUsage(inputTokens = 10, outputTokens = 5),
                    finishReason = LlmFinishReason.TOOL_CALLS,
                )
            }
            else -> throw IllegalStateException("LtmScriptedLlmClient: unknown response type ${entry.javaClass}")
        }
    }

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): Flow<LlmStreamEvent> = flow {
        throw UnsupportedOperationException("LtmScriptedLlmClient: streaming not used in these tests")
    }

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: LlmToolExecutor,
    ): LlmResponse = throw UnsupportedOperationException(
        "ScriptedLlmClient: chatCompletionWithTools not used; ReActLoop drives chatCompletion"
    )

    private fun extractPriorToolResults(messages: List<LlmMessage>): List<Map<String, Any?>> {
        val gson = Gson()
        val out = ArrayList<Map<String, Any?>>()
        for (msg in messages) {
            if (msg.role == LlmMessageRole.TOOL) {
                val text = msg.textContent()
                @Suppress("UNCHECKED_CAST")
                val parsed: Map<String, Any?> = runCatching {
                    gson.fromJson(text, Map::class.java) as Map<String, Any?>
                }.getOrDefault(emptyMap())
                out.add(parsed)
            }
        }
        return out
    }
}

/** A scripted response that builds its tool-call args from prior tool results. */
private class LtmResolverEntry(
    val toolName: String,
    val toolCallId: String,
    val argBuilder: (priorResults: List<Map<String, Any?>>) -> Map<String, Any?>,
)

/** An [LlmClient] whose every method throws — used when a test must assert the LLM is never called. */
private class LtmUnsupportedLlmClient : LlmClient {
    override suspend fun chatCompletion(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): LlmResponse = throw UnsupportedOperationException("LtmUnsupportedLlmClient: not expected to be called")

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): Flow<LlmStreamEvent> = flow {
        throw UnsupportedOperationException("LtmUnsupportedLlmClient: streaming not expected")
    }

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: LlmToolExecutor,
    ): LlmResponse = throw UnsupportedOperationException("LtmUnsupportedLlmClient: not expected to be called")
}

/**
 * Throwaway test-only [RoomDatabase] that registers ONLY the four mem entities.
 *
 * Same pattern as `MemRetrievalTest.MemRetrievalTestDb`; declared separately so
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
abstract class LtmToolsTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
