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
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.llm.LlmTokenUsage
import java.time.ZoneId
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
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
 * Tests for [ExtractionPipeline.ingest] — Kotlin port of the behavior in
 * `mem/extract.py::ExtractionPipeline.ingest`.
 *
 * A hand-written [ScriptedLlmClient] implements the real [LlmClient] interface
 * and returns canned [LlmResponse]s in sequence: each response either carries
 * tool calls (driving the [ReActLoop] forward) or a plain-text final answer
 * (ending the loop). The fake records how many times [chatCompletion] is
 * invoked so the idempotent-replay test can assert the LLM is NOT called on a
 * second ingest of an already-done batch.
 *
 * Coverage:
 * - Full happy-path ingest: register_entity → register_content → link_event_entity → done.
 *   Verifies nodes/links/receipt persisted, receipt status=done.
 * - Idempotent replay: a second ingest of the same batchId returns the old
 *   receipt WITHOUT calling the LLM (call count stays at the first run's value).
 * - Hallucinated-hash rejection: a link_event_entity with an entity hash that
 *   was never returned by any tool call is rejected by the tool handler, the
 *   loop's validation budget absorbs it, and the batch ultimately fails.
 * - Repair-round path: the first run leaves an entity mention unlinked; the
 *   closing validation feeds back, the second run adds the missing link, and
 *   the batch succeeds.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ExtractionPipelineTest {

    private lateinit var db: ExtractionPipelineTestDb
    private lateinit var store: MemStore
    private lateinit var embedder: PipelineDeterministicEmbedder
    private lateinit var registry: NodeTypeRegistry
    private lateinit var config: LlmGenerationConfig

    private val zone: ZoneId = ZoneId.of("Asia/Shanghai")
    private val mentionTime = 1_700_000_000L

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            ExtractionPipelineTestDb::class.java,
        ).allowMainThreadQueries().build()
        store = MemStore(
            nodeDao = db.memNodeDao(),
            linkDao = db.memLinkDao(),
            vectorDao = db.memVectorDao(),
            receiptDao = db.memReceiptDao(),
            expectedDim = 4,
            transactionRunner = null,
            zoneId = zone,
        )
        runBlocking { store.load() }
        embedder = PipelineDeterministicEmbedder(dim = 4)
        registry = NodeTypeRegistry()
        config = LlmGenerationConfig(model = "test-model")
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // Happy path
    // ------------------------------------------------------------------

    @Test
    fun `full ingest registers entity, content, link and returns a done receipt`() = runBlocking {
        // The fake's first response carries a register_entity tool call.
        // We don't know the hash ahead of time, so the fake uses a callback
        // pattern: it inspects the tool results already in the conversation
        // to fill in hashes for subsequent calls.
        val llm = ScriptedLlmClient(
            responses = listOf(
                // Turn 1: register the entity.
                toolCallResponse(
                    LlmToolCall(
                        id = "call-1",
                        name = "register_entity",
                        argumentsJson = """{"name":"Alice","entity_type":"person","description":"a friend","aliases":["A"]}""",
                    ),
                ),
                // Turn 2: register content mentioning Alice.
                toolCallResponse(
                    LlmToolCall(
                        id = "call-2",
                        name = "register_content",
                        argumentsJson = """{"node_type":"event","content":"Alice visited","entity_mentions":["Alice"],"event_time":"2026-08-08"}""",
                    ),
                ),
                // Turn 3: link content to entity. Hashes are filled by the
                // fake's resolver from prior tool results.
                resolverToolCallResponse(
                    toolName = "link_event_entity",
                    toolCallId = "call-3",
                    argBuilder = { priorResults ->
                        // priorResults is a list of maps: [{"hash": "..."}, ...]
                        val entityHash = priorResults[0]["hash"] as String
                        val contentHash = priorResults[1]["hash"] as String
                        mapOf(
                            "content_hash" to contentHash,
                            "entity_hash" to entityHash,
                            "role" to "主角",
                        )
                    },
                ),
                // Turn 4: plain-text final answer.
                finalTextResponse("Stored one event about Alice visiting."),
            ),
        )
        val pipeline = ExtractionPipeline(
            store = store,
            llm = llm,
            config = config,
            embedder = embedder,
            registry = registry,
        )
        val batch = batch(text = "Alice visited yesterday")

        val receipt = pipeline.ingest(batch)

        assertEquals(ReceiptStatus.DONE, receipt.status)
        assertEquals("manual", receipt.source)
        assertNull(receipt.error)
        assertEquals(2, receipt.nodeHashes.size) // entity + content
        assertEquals(1, receipt.linkCount)
        // The receipt is persisted.
        val stored = store.getReceipt(batch.batchId)
        assertNotNull(stored)
        assertEquals(ReceiptStatus.DONE, stored!!.status)
        // The nodes are in the store.
        for (hash in receipt.nodeHashes) {
            assertNotNull(store.getNode(hash))
        }
        // Stats carry turn and tool-call counts.
        assertEquals(4, receipt.stats["turns"])
        assertEquals(3, receipt.stats["tool_calls"])
    }

    // ------------------------------------------------------------------
    // Idempotent replay
    // ------------------------------------------------------------------

    @Test
    fun `second ingest of an already-done batch returns the old receipt without calling the LLM`() = runBlocking {
        val llm = ScriptedLlmClient(
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
            store = store, llm = llm, config = config, embedder = embedder, registry = registry,
        )
        val batch = batch(text = "Bob did something")

        val first = pipeline.ingest(batch)
        assertEquals(ReceiptStatus.DONE, first.status)
        val callsAfterFirst = llm.callCount

        // Second ingest: should short-circuit on the done receipt.
        val second = pipeline.ingest(batch)
        assertEquals(ReceiptStatus.DONE, second.status)
        assertEquals(first.nodeHashes, second.nodeHashes)
        assertEquals(callsAfterFirst, llm.callCount)
    }

    // ------------------------------------------------------------------
    // Hallucinated-hash rejection
    // ------------------------------------------------------------------

    @Test
    fun `link_event_entity with a hallucinated entity hash is rejected and the batch fails`() = runBlocking {
        val llm = ScriptedLlmClient(
            responses = listOf(
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "register_content",
                        argumentsJson = """{"node_type":"event","content":"event with fake entity","entity_mentions":["Nobody"]}""",
                    ),
                ),
                // Turn 2: try to link the staged content to a hallucinated
                // entity hash. The content hash is resolved from the prior
                // tool result; the entity hash is invented.
                ResolverToolCallResponse(
                    toolName = "link_event_entity",
                    toolCallId = "c2",
                    argBuilder = { prior ->
                        val contentHash = prior[0]["hash"] as String
                        mapOf(
                            "content_hash" to contentHash,
                            "entity_hash" to "HALLUCINATED12",
                        )
                    },
                ),
                // Turn 3: the model gives up with a final text (the link was
                // rejected; the mention stays unlinked).
                finalTextResponse("Could not link."),
            ),
        )
        val pipeline = ExtractionPipeline(
            store = store, llm = llm, config = config, embedder = embedder, registry = registry,
            maxValidationRetries = 0,
        )
        val batch = batch(text = "event with fake entity")

        val receipt = pipeline.ingest(batch)

        // Closing validation fails: the content mentions "Nobody" but no link
        // was staged (the hallucinated hash was rejected by the tool handler).
        assertEquals(ReceiptStatus.FAILED, receipt.status)
        assertNotNull(receipt.error)
        // Nothing committed.
        assertEquals(0, receipt.nodeHashes.size)
    }

    // ------------------------------------------------------------------
    // Repair-round path
    // ------------------------------------------------------------------

    @Test
    fun `unlinked mention triggers a repair round that adds the missing link`() = runBlocking {
        // First run: register entity + content (mentioning the entity), but
        // forget to link. The loop ends with a final text.
        // Closing validation detects the unlinked mention and feeds back.
        // Second run: the model adds the missing link and ends again.
        val llm = ScriptedLlmClient(
            responses = listOf(
                // === First loop run ===
                toolCallResponse(
                    LlmToolCall(
                        id = "r1-c1",
                        name = "register_entity",
                        argumentsJson = """{"name":"Carol","entity_type":"person","description":"a friend"}""",
                    ),
                ),
                toolCallResponse(
                    LlmToolCall(
                        id = "r1-c2",
                        name = "register_content",
                        argumentsJson = """{"node_type":"event","content":"Carol called","entity_mentions":["Carol"]}""",
                    ),
                ),
                finalTextResponse("Stored Carol called."), // ← ends first run, mention unlinked
                // === Second loop run (after repair feedback) ===
                resolverToolCallResponse(
                    toolName = "link_event_entity",
                    toolCallId = "r2-c1",
                    argBuilder = { prior ->
                        // prior holds results from the FIRST run only (the
                        // second run is a fresh loop.run with a fresh seed).
                        // But the LoopClient splices the seed, so the
                        // conversation history carries the prior tool results.
                        // The resolver inspects the full message history.
                        mapOf(
                            "content_hash" to prior[1]["hash"] as String,
                            "entity_hash" to prior[0]["hash"] as String,
                        )
                    },
                ),
                finalTextResponse("Linked Carol."),
            ),
        )
        val pipeline = ExtractionPipeline(
            store = store, llm = llm, config = config, embedder = embedder, registry = registry,
            maxValidationRetries = 2,
        )
        val batch = batch(text = "Carol called")

        val receipt = pipeline.ingest(batch)

        assertEquals(ReceiptStatus.DONE, receipt.status)
        assertEquals(1, receipt.stats["validation_retries"])
        assertEquals(1, receipt.linkCount)
    }

    // ------------------------------------------------------------------
    // Per-batchId mutex (concurrent same-batchId ingests serialize)
    // ------------------------------------------------------------------

    @Test
    fun `concurrent ingests of the same batchId serialize on a per-batchId lock`() = runBlocking {
        // A blocking fake: the first chatCompletion signals it has started (so the test can
        // launch the second ingest while the first is still in the agent loop), then blocks on
        // a gate latch until the test opens it. The second ingest must NOT reach chatCompletion
        // while the first holds the per-batchId mutex — it blocks on the mutex, and once the
        // first finishes (writes the done receipt) the second observes it and short-circuits.
        // Assertion: chatCompletion is invoked exactly once across both ingests.
        val startedLatch = CountDownLatch(1)
        val gateLatch = CountDownLatch(1)
        val llm = BlockingScriptedLlmClient(
            responses = listOf(
                toolCallResponse(
                    LlmToolCall(
                        id = "x1",
                        name = "register_entity",
                        argumentsJson = """{"name":"Dave","entity_type":"person","description":"x"}""",
                    ),
                ),
                toolCallResponse(
                    LlmToolCall(
                        id = "x2",
                        name = "register_content",
                        argumentsJson = """{"node_type":"event","content":"Dave did X","entity_mentions":["Dave"]}""",
                    ),
                ),
                resolverToolCallResponse(
                    toolName = "link_event_entity",
                    toolCallId = "x3",
                    argBuilder = { prior ->
                        mapOf(
                            "content_hash" to prior[1]["hash"] as String,
                            "entity_hash" to prior[0]["hash"] as String,
                        )
                    },
                ),
                finalTextResponse("done"),
            ),
            startedLatch = startedLatch,
            gateLatch = gateLatch,
        )
        val pipeline = ExtractionPipeline(
            store = store, llm = llm, config = config, embedder = embedder, registry = registry,
        )
        val batch = batch(text = "Dave did X")

        coroutineScope {
            // Run the ingests on Dispatchers.IO (real threads) so the blocking CountDownLatch
            // calls in the fake LLM do not starve runBlocking's single-threaded event loop.
            val first = async(Dispatchers.IO) { pipeline.ingest(batch) }
            // Wait until the first ingest is inside the agent loop (chatCompletion started).
            assertTrue("first ingest did not start within timeout", startedLatch.await(5, TimeUnit.SECONDS))
            // Launch the second concurrent ingest of the SAME batchId. It must block on the
            // per-batchId mutex; give it a moment to ensure it has queued on the lock.
            val second = async(Dispatchers.IO) {
                delay(100) // let the second ingest reach the mutex and block
                pipeline.ingest(batch)
            }
            // Now release the first ingest's agent loop so it can finish and write the receipt.
            gateLatch.countDown()
            val results = listOf(first, second).awaitAll()
            val r1 = results[0]
            val r2 = results[1]
            assertEquals(ReceiptStatus.DONE, r1.status)
            assertEquals(ReceiptStatus.DONE, r2.status)
            // The first ingest drove the full agent loop (4 scripted LLM calls: register_entity,
            // register_content, link_event_entity, final text). The second ingest must have
            // short-circuited on the done receipt — so the total LLM call count is exactly 4,
            // not 8. This proves the per-batchId mutex serialized the two ingests.
            assertEquals(4, llm.callCount.get())
        }
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun batch(text: String): IngestBatch =
        IngestBatch(
            batchId = "batch-" + text.hashCode(),
            source = BatchSource.MANUAL,
            text = text,
            timeAnchor = 1_700_000_000L,
            mentionTime = mentionTime,
        )

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

    /**
     * Build an [LlmResponse] carrying one tool call whose arguments are
     * resolved at call time from the prior tool results in the conversation.
     */
    private fun resolverToolCallResponse(
        toolName: String,
        toolCallId: String,
        argBuilder: (priorResults: List<Map<String, Any?>>) -> Map<String, Any?>,
    ): ResolverToolCallResponse = ResolverToolCallResponse(toolName, toolCallId, argBuilder)
}

/**
 * Scripted [LlmClient] fake that returns canned [LlmResponse]s in sequence.
 *
 * Each entry in [responses] is returned once per [chatCompletion] call, in
 * order. A [ResolverToolCallResponse] entry inspects the conversation's prior
 * tool-result messages to build its arguments dynamically (so the fake can
 * reference hashes returned by earlier tool calls without knowing them ahead
 * of time).
 *
 * The fake implements all three [LlmClient] methods; only [chatCompletion] is
 * driven by the [ReActLoop] (the other two delegate to a stub that is never
 * reached in these tests).
 */
private class ScriptedLlmClient(
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
            throw IllegalStateException("ScriptedLlmClient: no more scripted responses")
        }
        val entry = queue.removeFirst()
        return when (entry) {
            is LlmResponse -> entry
            is ResolverToolCallResponse -> {
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
            else -> throw IllegalStateException("ScriptedLlmClient: unknown response type ${entry.javaClass}")
        }
    }

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): Flow<LlmStreamEvent> = flow {
        throw UnsupportedOperationException("ScriptedLlmClient: streaming not used in these tests")
    }

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: LlmToolExecutor,
    ): LlmResponse =
        throw UnsupportedOperationException("ScriptedLlmClient: chatCompletionWithTools not used; ReActLoop drives chatCompletion")

    /**
     * Extract the parsed JSON result of every prior tool message in the
     * conversation, in order. Used by [ResolverToolCallResponse] to fill in
     * hashes returned by earlier tool calls.
     */
    private fun extractPriorToolResults(messages: List<LlmMessage>): List<Map<String, Any?>> {
        val gson = Gson()
        val out = ArrayList<Map<String, Any?>>()
        for (msg in messages) {
            if (msg.role == com.l2dchat.core.llm.LlmMessageRole.TOOL) {
                val text = msg.textContent()
                val parsed: Map<String, Any?> =
                    runCatching {
                        @Suppress("UNCHECKED_CAST")
                        gson.fromJson(text, Map::class.java) as Map<String, Any?>
                    }.getOrDefault(emptyMap())
                out.add(parsed)
            }
        }
        return out
    }
}

/** A scripted response that builds its tool-call args from prior tool results. */
private class ResolverToolCallResponse(
    val toolName: String,
    val toolCallId: String,
    val argBuilder: (priorResults: List<Map<String, Any?>>) -> Map<String, Any?>,
)

/**
 * [ScriptedLlmClient] variant that blocks on the first [chatCompletion] call until a gate
 * latch is opened, and signals a started latch when that first call begins. Used by the
 * per-batchId mutex test to hold the first ingest inside the agent loop while the second
 * concurrent same-batchId ingest is launched — proving the second blocks on the mutex and
 * later short-circuits on the done receipt instead of calling the LLM a second time.
 */
private class BlockingScriptedLlmClient(
    private val responses: List<Any>,
    private val startedLatch: CountDownLatch,
    private val gateLatch: CountDownLatch,
) : LlmClient {
    val callCount: AtomicInteger = AtomicInteger(0)
    private val queue: ArrayDeque<Any> = ArrayDeque(responses)

    override suspend fun chatCompletion(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): LlmResponse {
        val n = callCount.incrementAndGet()
        if (n == 1) {
            // First call: signal the test, then block until released so the second ingest can
            // be launched while this one is still mid-loop.
            startedLatch.countDown()
            gateLatch.await(10, TimeUnit.SECONDS)
        }
        if (queue.isEmpty()) {
            throw IllegalStateException("BlockingScriptedLlmClient: no more scripted responses")
        }
        val entry = queue.removeFirst()
        return when (entry) {
            is LlmResponse -> entry
            is ResolverToolCallResponse -> {
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
            else -> throw IllegalStateException("BlockingScriptedLlmClient: unknown response type ${entry.javaClass}")
        }
    }

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): Flow<LlmStreamEvent> = flow {
        throw UnsupportedOperationException("BlockingScriptedLlmClient: streaming not used")
    }

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: LlmToolExecutor,
    ): LlmResponse =
        throw UnsupportedOperationException("BlockingScriptedLlmClient: chatCompletionWithTools not used")

    private fun extractPriorToolResults(messages: List<LlmMessage>): List<Map<String, Any?>> {
        val gson = Gson()
        val out = ArrayList<Map<String, Any?>>()
        for (msg in messages) {
            if (msg.role == com.l2dchat.core.llm.LlmMessageRole.TOOL) {
                val text = msg.textContent()
                @Suppress("UNCHECKED_CAST")
                val parsed: Map<String, Any?> =
                    runCatching {
                        gson.fromJson(text, Map::class.java) as Map<String, Any?>
                    }.getOrDefault(emptyMap())
                out.add(parsed)
            }
        }
        return out
    }
}

/**
 * Deterministic embedder for tests: derives a fixed-dimension vector from the
 * text's hash code so no queueing is needed.
 */
private class PipelineDeterministicEmbedder(private val dim: Int) : EmbeddingClient {
    override val dimension: Int = dim

    override suspend fun embed(texts: List<String>): List<FloatArray> =
        texts.map { text ->
            val base = text.hashCode()
            FloatArray(dim) { i -> ((base shr (i * 3)) and 0xFF).toFloat() / 255f }
        }
}

/**
 * Throwaway test-only [RoomDatabase] for [ExtractionPipelineTest].
 *
 * Same pattern as `MemStoreTest.MemStoreTestDb`; declared separately so this
 * test class is self-contained and does not collide with another test file's
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
abstract class ExtractionPipelineTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
