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
 * Tests for [MemSearchAgent] — Kotlin port of `mem/search_agent.py`.
 *
 * Drives the search sub agent's ReAct loop with a scripted [LlmClient]
 * ([MemSearchScriptedLlmClient]) against a throwaway test-only
 * [MemSearchAgentTestDb] (same pattern as [LtmToolsTest]'s `LtmToolsTestDb`).
 * The fixture is seeded via [TxWriter] (the production write path) with a
 * [MemSearchFakeEmbedder] that returns deterministic queued vectors, so the
 * cosine ranking inside the retrieval tools is fully reproducible.
 *
 * Coverage (per phase-8 task spec):
 * - Happy path: question → `search` call → `expand` call → final answer with
 *   inline hash citations; `error = null`; stats carry turns / tool_calls /
 *   usage.
 * - Budget exhaustion: loop hits `maxTurns` without producing a final answer →
 *   `error` starts with `"search agent aborted"`, stats.turns == maxTurns.
 * - Provider failure: the LLM throws on `chatCompletion` → `error` starts with
 *   `"search agent failed"`, `ask` never throws.
 * - Empty question: short-circuits to `error = "empty question"` without
 *   invoking the LLM.
 * - Usage accumulation: token counters across multiple turns sum correctly.
 * - Dual-channel error semantics: a bad-arg tool call (e.g. `search` with a
 *   non-string `query`) is a validation failure that burns the loop's
 *   consecutive-failure budget (the model is forced to correct itself),
 *   while a data problem (no results) goes into the payload without burning
 *   the budget.
 *
 * All file-local helper classes are `MemSearch`-prefixed to avoid the private
 * class-name collisions that bit phase 6a when multiple test files declared
 * same-named fakes.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class MemSearchAgentTest {

    private lateinit var db: MemSearchAgentTestDb
    private lateinit var store: MemStore
    private lateinit var embedder: MemSearchFakeEmbedder
    private lateinit var retrieval: MemRetrieval
    private lateinit var config: LlmGenerationConfig

    private val zone: ZoneId = ZoneId.of("Asia/Shanghai")
    private val fixedNow: Long = 1_786_116_600L // 2026-08-07 23:30 UTC+8

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            MemSearchAgentTestDb::class.java,
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
        embedder = MemSearchFakeEmbedder(dimension = 3)
        retrieval = MemRetrieval(
            store = store,
            embedder = embedder,
            zoneId = zone,
            nowFn = { fixedNow },
        )
        config = LlmGenerationConfig(model = "search-test-model")
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // Happy path: question → search → expand → final answer
    // ------------------------------------------------------------------

    @Test
    fun `ask runs a multi-round search loop and returns a cited answer`() = runBlocking {
        val h = putEvent("Alice visited the park", vector = listOf(1f, 0f, 0f))
        // The retrieval `search` tool will call embedder.embed(query); queue
        // the matching vector so cosine recall finds the seeded node.
        embedder.queue(listOf(1f, 0f, 0f))

        val llm = MemSearchScriptedLlmClient(
            responses = listOf(
                // Turn 1: model calls `search`.
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "search",
                        argumentsJson = """{"query":"park","k":5}""",
                    ),
                ),
                // Turn 2: model calls `expand` on the hash it just saw.
                expandCallResponse(toolCallId = "c2", priorHashIndex = 0),
                // Turn 3: model produces the final cited answer.
                finalTextResponse("Alice visited the park [${h.take(8)}]."),
            ),
        )
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
        )

        val answer = agent.ask("What did Alice do?")

        assertNull(answer.error)
        assertTrue(
            "answer must contain the hash citation: '${answer.answer}'",
            answer.answer.contains(h.take(8)),
        )
        assertEquals(3, answer.stats.turns)
        assertEquals(
            "two tool calls (search + expand) must be recorded",
            2,
            answer.stats.toolCalls,
        )
        // Usage accumulates across all three LLM turns (10 + 5 each).
        assertEquals(
            "prompt_tokens must accumulate across turns",
            30,
            answer.stats.usage.getValue("prompt_tokens"),
        )
        assertEquals(
            "completion_tokens must accumulate across turns",
            15,
            answer.stats.usage.getValue("completion_tokens"),
        )
        assertEquals(
            "total_tokens must accumulate across turns",
            45,
            answer.stats.usage.getValue("total_tokens"),
        )
    }

    // ------------------------------------------------------------------
    // Budget exhaustion: loop hits maxTurns → aborted error
    // ------------------------------------------------------------------

    @Test
    fun `ask reports an aborted error when the loop exhausts maxTurns`() = runBlocking {
        putEvent("seed", vector = listOf(1f, 0f, 0f))
        // Every turn the model emits a search call; the loop never gets a
        // final-text response, so it runs out of turns and raises
        // ReactLoopAbort. Queue enough vectors for each search call.
        repeat(3) { embedder.queue(listOf(1f, 0f, 0f)) }

        val llm = MemSearchScriptedLlmClient(
            responses = List(3) { i ->
                toolCallResponse(
                    LlmToolCall(
                        id = "c$i",
                        name = "search",
                        argumentsJson = """{"query":"loop","k":3}""",
                    ),
                )
            },
        )
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
            maxTurns = 3,
        )

        val answer = agent.ask("keep looping")

        assertTrue(
            "error must mention abort: '${answer.error}'",
            answer.error!!.startsWith("search agent aborted"),
        )
        assertEquals("", answer.answer)
        assertEquals(
            "stats.turns must be capped at maxTurns",
            3,
            answer.stats.turns,
        )
        assertEquals(3, answer.stats.toolCalls)
    }

    // ------------------------------------------------------------------
    // Provider failure: LLM throws → failed error, ask never throws
    // ------------------------------------------------------------------

    @Test
    fun `ask reports a failed error when the llm throws`() = runBlocking {
        val llm = MemSearchThrowingLlmClient()
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
        )

        val answer = agent.ask("anything")

        assertTrue(
            "error must mention failure: '${answer.error}'",
            answer.error!!.startsWith("search agent failed"),
        )
        assertEquals("", answer.answer)
    }

    // ------------------------------------------------------------------
    // Empty question: short-circuits without invoking the LLM
    // ------------------------------------------------------------------

    @Test
    fun `ask with an empty question returns an error without calling the llm`() = runBlocking {
        val llm = MemSearchThrowingLlmClient() // would fail the test if called
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
        )

        val answer = agent.ask("   ")

        assertEquals("", answer.answer)
        assertEquals("empty question", answer.error)
        assertEquals(0, answer.stats.turns)
        assertEquals(0, answer.stats.toolCalls)
    }

    @Test
    fun `ask normalizes whitespace before the empty check`() = runBlocking {
        val h = putEvent("normalized-event", vector = listOf(1f, 0f, 0f))
        embedder.queue(listOf(1f, 0f, 0f))

        val llm = MemSearchScriptedLlmClient(
            responses = listOf(
                finalTextResponse("ok [${h.take(8)}]"),
            ),
        )
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
        )

        val answer = agent.ask("  what\nhappened\trecently  ")

        assertNull(answer.error)
        assertTrue(answer.answer.contains(h.take(8)))
    }

    // ------------------------------------------------------------------
    // Dual-channel error semantics: bad-arg burns budget, data miss doesn't
    // ------------------------------------------------------------------

    @Test
    fun `bad-arg tool call is a validation failure that burns the loop budget`() = runBlocking {
        // The model calls `search` with a non-string query. The handler
        // throws ToolValidationError → the loop logs a validation failure and
        // feeds back. Two consecutive validation failures abort the loop.
        putEvent("seed", vector = listOf(1f, 0f, 0f))

        val llm = MemSearchScriptedLlmClient(
            responses = listOf(
                // Turn 1: bad-arg call (non-string query).
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "search",
                        argumentsJson = """{"query": 123}""",
                    ),
                ),
                // Turn 2: another bad-arg call → second consecutive failure
                // → ReactLoopAbort.
                toolCallResponse(
                    LlmToolCall(
                        id = "c2",
                        name = "search",
                        argumentsJson = """{"query": 456}""",
                    ),
                ),
            ),
        )
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
            maxTurns = 5,
        )

        val answer = agent.ask("force validation failures")

        assertNotNull(answer.error)
        assertTrue(
            "two consecutive validation failures must abort: '${answer.error}'",
            answer.error!!.startsWith("search agent aborted"),
        )
    }

    @Test
    fun `data miss goes into the payload without burning the budget`() = runBlocking {
        // The model calls `expand` with an unknown prefix. The handler
        // returns {"node": null, "error": "..."} (NOT a ToolValidationError),
        // so the loop's consecutive-failure counter resets and the model can
        // continue to a final answer.
        val llm = MemSearchScriptedLlmClient(
            responses = listOf(
                // Turn 1: expand with an unknown prefix → data error payload.
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "expand",
                        argumentsJson = """{"hash":"ZZZZZZZZZZ"}""",
                    ),
                ),
                // Turn 2: model gives a final answer despite the data miss.
                finalTextResponse("Memory has nothing relevant."),
            ),
        )
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
        )

        val answer = agent.ask("anything")

        assertNull(answer.error)
        assertEquals("Memory has nothing relevant.", answer.answer)
        assertEquals(2, answer.stats.turns)
        assertEquals(1, answer.stats.toolCalls)
    }

    // ------------------------------------------------------------------
    // extra_tools collision guard
    // ------------------------------------------------------------------

    @Test
    fun `extra_tools colliding with built-in tool names are rejected at construction`() {
        val collision = ToolDef(
            schema = LlmToolDefinition(
                name = "search",
                description = "colliding tool",
                parameters = mapOf("type" to "object", "properties" to emptyMap<String, Any>()),
            ),
            handler = { emptyMap<String, Any?>() },
        )
        assertThrows<IllegalArgumentException> {
            MemSearchAgent(
                store = store,
                embedder = embedder,
                llm = MemSearchThrowingLlmClient(),
                config = config,
                zoneId = zone,
                nowFn = { fixedNow },
                extraTools = mapOf("search" to collision),
            )
        }
    }

    @Test
    fun `extra_tools with non-colliding names are merged into the tool surface`() = runBlocking {
        val extra = ToolDef(
            schema = LlmToolDefinition(
                name = "kb_search",
                description = "host knowledge base",
                parameters = mapOf("type" to "object", "properties" to emptyMap<String, Any>()),
            ),
            handler = { mapOf("results" to listOf("kb-hit"), "error" to null) },
        )
        val llm = MemSearchScriptedLlmClient(
            responses = listOf(
                toolCallResponse(
                    LlmToolCall(
                        id = "c1",
                        name = "kb_search",
                        argumentsJson = """{"query":"x"}""",
                    ),
                ),
                finalTextResponse("done"),
            ),
        )
        val agent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = llm,
            config = config,
            zoneId = zone,
            nowFn = { fixedNow },
            extraTools = mapOf("kb_search" to extra),
        )

        val answer = agent.ask("use the extra tool")

        assertNull(answer.error)
        assertEquals("done", answer.answer)
        assertEquals(1, answer.stats.toolCalls)
    }

    // ------------------------------------------------------------------
    // SearchAnswer.toDict payload shape
    // ------------------------------------------------------------------

    @Test
    fun `SearchAnswer toDict produces the planner payload shape`() {
        val answer = SearchAnswer(
            answer = "cited answer",
            error = null,
            stats = SearchStats(
                turns = 2,
                toolCalls = 1,
                usage = LinkedHashMap<String, Int>().apply {
                    put("prompt_tokens", 10)
                    put("completion_tokens", 5)
                    put("total_tokens", 15)
                },
            ),
        )
        val dict = answer.toDict()
        assertEquals("cited answer", dict["answer"])
        assertNull(dict["error"])
        @Suppress("UNCHECKED_CAST")
        val stats = dict["stats"] as Map<String, Any>
        assertEquals(2, stats["turns"])
        assertEquals(1, stats["tool_calls"])
        @Suppress("UNCHECKED_CAST")
        val usage = stats["usage"] as Map<String, Int>
        assertEquals(10, usage["prompt_tokens"])
        assertEquals(15, usage["total_tokens"])
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /** Fresh writer with the same dim as the store under test. */
    private fun freshWriter(): TxWriter = TxWriter(dimensions = 3)

    /** Insert an event node with an optional vector and event_time. */
    private suspend fun putEvent(
        content: String,
        eventTime: String? = null,
        vector: List<Float>? = null,
        mentionTime: Long = 1_000L,
    ): String {
        val w = freshWriter()
        val h = w.putNode(
            nodeType = "event",
            baseType = "content",
            content = content,
            eventTime = eventTime,
            mentionTime = mentionTime,
        )
        if (vector != null) w.putVector(h, vector)
        w.flush(store)
        return h
    }

    /** Build an [LlmResponse] carrying one tool call. */
    private fun toolCallResponse(call: LlmToolCall): LlmResponse =
        LlmResponse(
            message = LlmMessage.assistantToolCalls(listOf(call)),
            model = "search-test-model",
            usage = LlmTokenUsage(inputTokens = 10, outputTokens = 5),
            finishReason = LlmFinishReason.TOOL_CALLS,
        )

    /** Build a final-text [LlmResponse] that ends the loop. */
    private fun finalTextResponse(text: String): LlmResponse =
        LlmResponse(
            message = LlmMessage.assistant(text),
            model = "search-test-model",
            usage = LlmTokenUsage(inputTokens = 10, outputTokens = 5),
            finishReason = LlmFinishReason.STOP,
        )

    /**
     * Build a scripted [LlmResponse] entry that calls `expand` on the first
     * hash returned by the prior `search` tool result.
     *
     * The search tool returns `{"results": [{"hash": "...", ...}, ...], ...}`,
     * so the scripted client inspects the conversation's tool messages to
     * extract the hash and build the expand call's arguments.
     */
    private fun expandCallResponse(toolCallId: String, priorHashIndex: Int): MemSearchResolverEntry =
        MemSearchResolverEntry(
            toolName = "expand",
            toolCallId = toolCallId,
            argBuilder = { priorResults ->
                val firstResult = priorResults[priorHashIndex]
                @Suppress("UNCHECKED_CAST")
                val resultsList = firstResult["results"] as List<Map<String, Any?>>
                val hash = resultsList[0]["hash"] as String
                mapOf("hash" to hash)
            },
        )

    /** Re-implementation of JUnit's assertThrows (not available on Android JUnit). */
    private inline fun <reified T : Throwable> assertThrows(block: () -> Unit) {
        try {
            block()
        } catch (e: Throwable) {
            if (e is T) return
            throw AssertionError(
                "Expected ${T::class.java.simpleName} but got ${e.javaClass.simpleName}: $e",
            )
        }
        throw AssertionError("Expected ${T::class.java.simpleName} to be thrown, but nothing was thrown")
    }
}

/* ---------------------------------------------------------------------- */
/* Test-only fakes (file-local, MemSearch-prefixed to avoid collisions)   */
/* ---------------------------------------------------------------------- */

/**
 * Hand-written fake [EmbeddingClient] for [MemSearchAgentTest].
 *
 * Returns pre-queued deterministic vectors in FIFO order. Each call to [embed]
 * pops one queued vector; if the queue is empty when [embed] is called, the
 * test fails fast with an assertion. Same pattern as [LtmToolsTest]'s
 * `LtmFakeEmbedder`, renamed to avoid the private-class collisions that bit
 * phase 6a.
 */
private class MemSearchFakeEmbedder(override val dimension: Int) : EmbeddingClient {
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
                "MemSearchFakeEmbedder: no vector queued for embed call " +
                    "(texts=${texts.size}, first='${texts.firstOrNull()?.take(20)}')"
            )
        }
        val vec = queue.removeFirst()
        check(vec.size == dimension) {
            "MemSearchFakeEmbedder: queued vector dim ${vec.size} != expected $dimension"
        }
        return listOf(vec)
    }
}

/**
 * Scripted [LlmClient] fake that returns canned [LlmResponse]s in sequence.
 *
 * Same pattern as [LtmToolsTest]'s `LtmScriptedLlmClient` and
 * [ExtractionPipelineTest]'s `ScriptedLlmClient`, renamed to avoid private
 * class collisions. Supports two entry shapes: a plain [LlmResponse] (played
 * verbatim) and a [MemSearchResolverEntry] (builds its tool-call arguments
 * from the prior tool results already in the conversation — used when the
 * scripted turn needs to reference a hash returned by an earlier tool call).
 */
private class MemSearchScriptedLlmClient(
    responses: List<Any>,
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
            throw IllegalStateException("MemSearchScriptedLlmClient: no more scripted responses")
        }
        val entry = queue.removeFirst()
        return when (entry) {
            is LlmResponse -> entry
            is MemSearchResolverEntry -> {
                val priorResults = extractPriorToolResults(messages)
                val args = entry.argBuilder(priorResults)
                val argsJson = Gson().toJson(args)
                LlmResponse(
                    message = LlmMessage.assistantToolCalls(
                        listOf(
                            LlmToolCall(
                                id = entry.toolCallId,
                                name = entry.toolName,
                                argumentsJson = argsJson,
                            ),
                        ),
                    ),
                    model = "search-test-model",
                    usage = LlmTokenUsage(inputTokens = 10, outputTokens = 5),
                    finishReason = LlmFinishReason.TOOL_CALLS,
                )
            }
            else -> throw IllegalStateException(
                "MemSearchScriptedLlmClient: unknown response type ${entry.javaClass}"
            )
        }
    }

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): Flow<LlmStreamEvent> = flow {
        throw UnsupportedOperationException("MemSearchScriptedLlmClient: streaming not used in these tests")
    }

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: LlmToolExecutor,
    ): LlmResponse = throw UnsupportedOperationException(
        "MemSearchScriptedLlmClient: chatCompletionWithTools not used; ReActLoop drives chatCompletion"
    )

    private fun extractPriorToolResults(messages: List<LlmMessage>): List<Map<String, Any?>> {
        val gson = Gson()
        val out = ArrayList<Map<String, Any?>>()
        for (msg in messages) {
            if (msg.role == com.l2dchat.core.llm.LlmMessageRole.TOOL) {
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
private class MemSearchResolverEntry(
    val toolName: String,
    val toolCallId: String,
    val argBuilder: (priorResults: List<Map<String, Any?>>) -> Map<String, Any?>,
)

/** An [LlmClient] whose every method throws — used when a test must assert the LLM is never called. */
private class MemSearchThrowingLlmClient : LlmClient {
    override suspend fun chatCompletion(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): LlmResponse = throw IllegalStateException("MemSearchThrowingLlmClient: boom")

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): Flow<LlmStreamEvent> = flow {
        throw UnsupportedOperationException("MemSearchThrowingLlmClient: streaming not expected")
    }

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: LlmToolExecutor,
    ): LlmResponse = throw IllegalStateException("MemSearchThrowingLlmClient: boom")
}

/**
 * Throwaway test-only [RoomDatabase] that registers ONLY the four mem entities.
 *
 * Same pattern as [LtmToolsTest]'s `LtmToolsTestDb`; declared separately (and
 * `MemSearch`-prefixed) so this test class is self-contained and does not
 * collide with another test file's private database.
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
abstract class MemSearchAgentTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
