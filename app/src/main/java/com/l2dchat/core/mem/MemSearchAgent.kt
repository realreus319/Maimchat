package com.l2dchat.core.mem

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.llm.LlmToolDefinition
import java.time.ZoneId

/**
 * Standard ReAct search sub agent — Kotlin port of
 * `mem/search_agent.py::MemSearchAgent` (design section 5.4).
 *
 * The extraction model cannot judge importance — that needs a whole-store view.
 * [MemSearchAgent] is the library's answer: a bounded [ReActLoop] armed with
 * the section-5.1 retrieval API (plus optional host knowledge tools), taking a
 * natural-language question and returning a grounded, hash-cited answer.
 *
 * Hosts call [ask] directly, or expose it to their planner as one tool
 * (`ltm_ask`, see [buildLtmTools]). Simple one-shot lookups should NOT go
 * through it — the planner surface's `ltm_search` covers those without an LLM
 * round-trip per hop.
 *
 * ## Dual-channel error semantics (mirrors Python)
 *
 * Parameter-shape errors inside the retrieval tools **throw**
 * [ToolValidationError] — the [ReActLoop] logs them as validation failures and
 * they count against the loop's consecutive-failure budget, forcing the model
 * to correct itself. Data problems (no results, bad `time_range`, ambiguous
 * hash prefix, missing node) go into the tool's result payload as
 * `{"error": "..."}` — they do NOT burn the budget.
 *
 * ## `ask` never throws
 *
 * [ask] catches [ReactLoopAbort] (budget exhaustion / repeated validation
 * failure) and any other exception (provider/network failures) and reports
 * them in [SearchAnswer.error]. The caller — often itself an agent loop —
 * treats failure as data.
 *
 * ## `Long` epoch seconds
 *
 * All time fields are epoch seconds as [Long] (never [Double]/[Float]), per
 * the port-wide convention. The Python reference uses `float` and
 * `datetime.fromtimestamp`; [nowFn] returns [Long] and [Prompts.buildSearchPrompt]
 * formats the anchor via [ZoneId].
 *
 * @property store The memory store (read side only — this agent never writes).
 * @property embedder Query embedder for the semantic-search tool.
 * @property llm Chat client for the agent loop (a mid-size model suffices).
 *   Mirrors Python's `ChatClient`; the Kotlin [ReActLoop] drives
 *   [LlmClient.chatCompletion] directly.
 * @property config Generation config (model, temperature, …) forwarded to
 *   every [LlmClient.chatCompletion] call.
 * @property zoneId Timezone used by [Prompts.buildSearchPrompt] and the
 *   retrieval layer's time-range parsing. Defaults to [ZoneId.systemDefault]
 *   to mirror Python's `datetime.fromtimestamp`.
 * @property maxTurns ReAct loop turn budget per question. Defaults to
 *   [DEFAULT_MAX_TURNS] (10), matching Python.
 * @property nowFn Clock for the search prompt's time anchor and the rank math,
 *   injected for tests. Returns epoch seconds as [Long].
 * @property promptExtra Optional host protocol text appended to the system
 *   prompt (e.g. when to consult host knowledge tools).
 * @property extraTools Optional host-side tools (kb_search, websearch…), merged
 *   into the tool whitelist; names must not collide with the built-ins.
 */
class MemSearchAgent(
    private val store: MemStore,
    embedder: EmbeddingClient,
    private val llm: LlmClient,
    private val config: LlmGenerationConfig,
    private val zoneId: ZoneId = ZoneId.systemDefault(),
    private val maxTurns: Int = DEFAULT_MAX_TURNS,
    private val nowFn: () -> Long = { System.currentTimeMillis() / 1000L },
    private val promptExtra: String? = null,
    extraTools: Map<String, ToolDef>? = null,
) {
    init {
        require(maxTurns >= 1) { "MemSearchAgent: maxTurns must be >= 1, got $maxTurns" }
    }

    private val tools: Map<String, ToolDef> = run {
        val retrieval = MemRetrieval(
            store = store,
            embedder = embedder,
            zoneId = zoneId,
            nowFn = nowFn,
        )
        val builtins = buildRetrievalTools(retrieval, store)
        if (extraTools == null) {
            builtins
        } else {
            val collisions = extraTools.keys.intersect(builtins.keys)
            require(collisions.isEmpty()) {
                "extra_tools collide with built-in tools: ${collisions.sorted()}"
            }
            LinkedHashMap(builtins).apply { putAll(extraTools) }
        }
    }

    /**
     * Research [question] in memory and answer with hash citations.
     *
     * Never throws: loop aborts and unexpected failures come back in
     * [SearchAnswer.error]. The question is whitespace-normalized (collapsed
     * and trimmed) before entering the loop; an empty question short-circuits
     * to an error answer without invoking the LLM.
     *
     * @param question Natural-language question.
     * @return The outcome — [SearchAnswer.answer] carries the agent's closing
     *   text (with inline hash citations), [SearchAnswer.error] is `null` on
     *   success, and [SearchAnswer.stats] records `turns`, `tool_calls`, and
     *   accumulated `usage`.
     */
    suspend fun ask(question: String): SearchAnswer {
        val normalized = question.split(WHITESPACE_RUN).filter { it.isNotEmpty() }.joinToString(" ")
        if (normalized.isEmpty()) {
            return SearchAnswer(answer = "", error = "empty question")
        }

        val usageChat = UsageChat(llm)
        val loop = ReActLoop(
            llm = usageChat,
            tools = LinkedHashMap(tools),
            config = config,
            maxTurns = maxTurns,
        )
        val prompt = Prompts.buildSearchPrompt(
            nowEpoch = nowFn(),
            zoneId = zoneId,
            extra = promptExtra,
        )
        val userContent: List<LlmContentPart> = listOf(LlmTextPart(normalized))

        return try {
            val result = loop.run(prompt, userContent)
            SearchAnswer(
                answer = (result.finalContent ?: "").trim(),
                stats = SearchStats(
                    turns = result.turns,
                    toolCalls = result.calls.size,
                    usage = LinkedHashMap(usageChat.usage),
                ),
            )
        } catch (e: ReactLoopAbort) {
            SearchAnswer(
                answer = "",
                error = "search agent aborted: $e",
                stats = SearchStats(
                    turns = maxTurns,
                    toolCalls = e.calls.size,
                    usage = LinkedHashMap(usageChat.usage),
                ),
            )
        } catch (e: Throwable) {
            // Provider/network failures and any other unexpected error —
            // `ask` never raises (mirrors Python's broad `except Exception`).
            SearchAnswer(
                answer = "",
                error = "search agent failed: $e",
                stats = SearchStats(usage = LinkedHashMap(usageChat.usage)),
            )
        }
    }

    /* ------------------------------------------------------------------ */
    /* Tool surface: the section-5.1 retrieval API as four loop tools     */
    /* ------------------------------------------------------------------ */

    /**
     * Build the four retrieval tools the search loop may call.
     *
     * Mirrors `search_agent.py::_build_tools`. Each handler returns a
     * `Map<String, Any?>` payload; parameter-shape errors throw
     * [ToolValidationError] (the [ReActLoop] counts them against the
     * consecutive-failure budget), while data problems (empty results, bad
     * time_range, ambiguous prefix, missing node) go into the payload's
     * `error` field without burning the budget.
     */
    private fun buildRetrievalTools(
        retrieval: MemRetrieval,
        store: MemStore,
    ): Map<String, ToolDef> {
        val search = ToolDef(
            schema = LlmToolDefinition(
                name = "search",
                description = "Semantic search over memory nodes. Returns summaries " +
                    "(hash prefix, preview, times, entity names) — expand for " +
                    "full text.",
                parameters = mapOf(
                    "type" to "object",
                    "properties" to mapOf(
                        "query" to mapOf("type" to "string", "description" to "free-text query"),
                        "k" to mapOf("type" to "integer", "description" to "max results (default 8)"),
                        "time_range" to mapOf(
                            "type" to "string",
                            "description" to "normalized time filter, e.g. 2026-07 or a/b interval",
                        ),
                        "time_axis" to mapOf(
                            "type" to "string",
                            "enum" to listOf("event", "mention"),
                            "description" to "axis time_range filters on (default event)",
                        ),
                        "entity" to mapOf(
                            "type" to "string",
                            "description" to "restrict to nodes linked to this entity",
                        ),
                        "node_type" to mapOf(
                            "type" to "string",
                            "description" to "exact node_type filter",
                        ),
                    ),
                    "required" to listOf("query"),
                    "additionalProperties" to false,
                ),
            ),
            handler = { args -> searchTool(retrieval, args) },
        )

        val temporal = ToolDef(
            schema = LlmToolDefinition(
                name = "temporal",
                description = "Pure time-axis scan: what does memory hold for a period. " +
                    "axis=mention (when recorded) or event (when it happened).",
                parameters = mapOf(
                    "type" to "object",
                    "properties" to mapOf(
                        "time_range" to mapOf(
                            "type" to "string",
                            "description" to "normalized time expression, e.g. 2026-07",
                        ),
                        "axis" to mapOf(
                            "type" to "string",
                            "enum" to listOf("mention", "event"),
                            "description" to "which axis to scan (default mention)",
                        ),
                        "k" to mapOf("type" to "integer", "description" to "max results (default 20)"),
                        "node_type" to mapOf("type" to "string", "description" to "exact node_type filter"),
                    ),
                    "required" to listOf("time_range"),
                    "additionalProperties" to false,
                ),
            ),
            handler = { args -> temporalTool(retrieval, args) },
        )

        val entityEvents = ToolDef(
            schema = LlmToolDefinition(
                name = "entity_events",
                description = "Everything memory links to one entity (name, alias or " +
                    "hash), most recently mentioned first.",
                parameters = mapOf(
                    "type" to "object",
                    "properties" to mapOf(
                        "entity" to mapOf("type" to "string", "description" to "entity name/alias/hash"),
                        "k" to mapOf("type" to "integer", "description" to "max results (default 20)"),
                    ),
                    "required" to listOf("entity"),
                    "additionalProperties" to false,
                ),
            ),
            handler = { args -> entityEventsTool(retrieval, args) },
        )

        val expand = ToolDef(
            schema = LlmToolDefinition(
                name = "expand",
                description = "Full content + metadata + links of one node, by full hash " +
                    "or the 8-char prefix from a summary. Use only for " +
                    "summaries you actually cite.",
                parameters = mapOf(
                    "type" to "object",
                    "properties" to mapOf(
                        "hash" to mapOf("type" to "string", "description" to "full hash or ≥6-char prefix"),
                    ),
                    "required" to listOf("hash"),
                    "additionalProperties" to false,
                ),
            ),
            handler = { args -> expandTool(retrieval, store, args) },
        )

        return LinkedHashMap<String, ToolDef>().apply {
            put("search", search)
            put("temporal", temporal)
            put("entity_events", entityEvents)
            put("expand", expand)
        }
    }

    /* ------------------------------------------------------------------ */
    /* Tool handlers                                                      */
    /* ------------------------------------------------------------------ */

    /** `search` handler — semantic search with optional filters. */
    private suspend fun searchTool(
        retrieval: MemRetrieval,
        args: Map<String, Any?>,
    ): Map<String, Any?> {
        val query = args["query"]
        if (query !is String || query.isBlank()) {
            throw ToolValidationError("search: query must be a non-empty string")
        }
        val k = checkedK(args["k"])
        val timeRange = args["time_range"] as? String
        val timeAxis = (args["time_axis"] as? String).orEmpty().ifBlank { "event" }
        val entity = args["entity"] as? String
        val nodeType = args["node_type"] as? String
        return try {
            val results = retrieval.search(
                query = query.trim(),
                k = k,
                nodeType = nodeType?.takeIf { it.isNotBlank() },
                timeRange = timeRange?.takeIf { it.isNotBlank() },
                timeAxis = timeAxis,
                entity = entity?.takeIf { it.isNotBlank() },
            )
            mapOf("results" to results.map { it.toSummaryMap() }, "error" to null)
        } catch (e: ToolValidationError) {
            mapOf("results" to emptyList<Any>(), "error" to (e.message ?: "validation error"))
        }
    }

    /** `temporal` handler — pure time-axis scan. */
    private suspend fun temporalTool(
        retrieval: MemRetrieval,
        args: Map<String, Any?>,
    ): Map<String, Any?> {
        val timeRange = args["time_range"]
        if (timeRange !is String || timeRange.isBlank()) {
            throw ToolValidationError("temporal: time_range must be a non-empty string")
        }
        val k = checkedK(args["k"])
        val axis = (args["axis"] as? String).orEmpty().ifBlank { "mention" }
        val nodeType = args["node_type"] as? String
        return try {
            val results = retrieval.temporal(
                timeRange = timeRange.trim(),
                axis = axis,
                k = k,
                nodeType = nodeType?.takeIf { it.isNotBlank() },
            )
            mapOf("results" to results.map { it.toSummaryMap() }, "error" to null)
        } catch (e: ToolValidationError) {
            mapOf("results" to emptyList<Any>(), "error" to (e.message ?: "validation error"))
        }
    }

    /** `entity_events` handler — everything linked to one entity. */
    private suspend fun entityEventsTool(
        retrieval: MemRetrieval,
        args: Map<String, Any?>,
    ): Map<String, Any?> {
        val entity = args["entity"]
        if (entity !is String || entity.isBlank()) {
            throw ToolValidationError("entity_events: entity must be a non-empty string")
        }
        val k = checkedK(args["k"])
        val results = retrieval.entityEvents(entity.trim(), k = k)
        if (results.isEmpty()) {
            return mapOf(
                "results" to emptyList<Any>(),
                "error" to "entity not found or has no events: '$entity'",
            )
        }
        return mapOf("results" to results.map { it.toSummaryMap() }, "error" to null)
    }

    /** `expand` handler — full detail of one node by full hash or prefix. */
    private suspend fun expandTool(
        retrieval: MemRetrieval,
        store: MemStore,
        args: Map<String, Any?>,
    ): Map<String, Any?> {
        val hash = args["hash"]
        if (hash !is String || hash.trim().length < MIN_PREFIX) {
            throw ToolValidationError(
                "expand: hash must be a full hash or ≥$MIN_PREFIX-char prefix",
            )
        }
        val prefix = hash.trim()
        val matches = store.resolveHash(prefix)
        if (matches.isEmpty()) {
            return mapOf("node" to null, "error" to "no node matches hash prefix '$prefix'")
        }
        if (matches.size > 1) {
            return mapOf(
                "node" to null,
                "error" to "hash prefix '$prefix' is ambiguous " +
                    "(${matches.size} candidates: ${matches.take(5)}) — give more characters",
            )
        }
        val node = retrieval.expand(matches[0])
        if (node == null) {
            return mapOf("node" to null, "error" to "node not found: ${matches[0]}")
        }
        return mapOf("node" to node.toExpandedMap(), "error" to null)
    }

    /* ------------------------------------------------------------------ */
    /* Helpers                                                            */
    /* ------------------------------------------------------------------ */

    /**
     * Validate a result-count argument from the model.
     *
     * Mirrors `search_agent.py::_checked_k`. Booleans are rejected even though
     * they serialize as integers in some JSON libraries (Python's
     * `isinstance(k, bool)` guard). Non-integers and out-of-range values throw
     * [ToolValidationError] — the loop's validation budget absorbs them,
     * forcing the model to correct itself.
     *
     * @throws ToolValidationError When `raw` is not an integer in `[1, 50]`.
     */
    private fun checkedK(raw: Any?): Int {
        if (raw == null) return K_DEFAULT
        if (raw is Boolean) throw ToolValidationError("k must be an integer in [1, 50], got $raw")
        val k = when (raw) {
            is Int -> raw
            is Long -> raw.toInt()
            is Number -> raw.toInt()
            else -> throw ToolValidationError("k must be an integer in [1, 50], got $raw")
        }
        if (k !in K_MIN..K_MAX) {
            throw ToolValidationError("k must be an integer in [1, 50], got $raw")
        }
        return k
    }

    companion object {
        /** Default turn budget, matching `search_agent.py`'s `max_turns=10`. */
        const val DEFAULT_MAX_TURNS: Int = 10

        /** Hash prefixes below this length are not serviced. */
        const val MIN_PREFIX: Int = 6

        /** `k` default for the `search` tool, matching Python. */
        const val K_DEFAULT: Int = 8

        /** `k` lower bound for the retrieval tools. */
        const val K_MIN: Int = 1

        /** `k` upper bound for the retrieval tools (matches Python's `[1, 50]`). */
        const val K_MAX: Int = 50

        /** Whitespace runs collapsed by [ask]'s normalization step. */
        private val WHITESPACE_RUN: Regex = Regex("\\s+")
    }
}

/**
 * Outcome of one [MemSearchAgent.ask] — Kotlin port of
 * `search_agent.py::SearchAnswer` (design section 5.4).
 *
 * @property answer The agent's closing text, with inline hash citations. Empty
 *   when [error] is non-null.
 * @property error Failure description, or `null`. [ask] never raises — the
 *   browser/LLM/loop all report here so the caller (often itself an agent loop)
 *   treats failure as data.
 * @property stats Turn / tool-call / token-usage counters.
 */
data class SearchAnswer(
    val answer: String,
    val error: String? = null,
    val stats: SearchStats = SearchStats(),
) {
    /**
     * Payload shape for agent-loop consumption.
     *
     * Mirrors `SearchAnswer.to_dict()` in Python: `{"answer", "error", "stats"}`.
     * The `stats` map is a snapshot copy so later mutations of the source do
     * not leak into the serialized payload.
     */
    fun toDict(): Map<String, Any?> = mapOf(
        "answer" to answer,
        "error" to error,
        "stats" to stats.toMap(),
    )
}

/**
 * Turn / tool-call / token-usage counters for one [MemSearchAgent.ask].
 *
 * Mirrors the `stats` dict in Python's `SearchAnswer`. The [usage] map carries
 * the three OpenAI token keys (`prompt_tokens`, `completion_tokens`,
 * `total_tokens`) accumulated across every LLM turn of the search loop.
 *
 * @property turns LLM turns used (capped at the agent's `maxTurns`).
 * @property toolCalls Number of tool calls executed by the loop.
 * @property usage Accumulated token usage. Keys: `prompt_tokens`,
 *   `completion_tokens`, `total_tokens`.
 */
data class SearchStats(
    val turns: Int = 0,
    val toolCalls: Int = 0,
    val usage: Map<String, Int> = emptyMap(),
) {
    /** Snapshot the stats as a plain map for payload serialization. */
    fun toMap(): Map<String, Any> = mapOf(
        "turns" to turns,
        "tool_calls" to toolCalls,
        "usage" to LinkedHashMap(usage),
    )
}

/* ---------------------------------------------------------------------- */
/* Internal helpers                                                       */
/* ---------------------------------------------------------------------- */

/**
 * Accumulate token usage across turns — Kotlin port of
 * `search_agent.py::_UsageChat`.
 *
 * The [ReActLoop] discards per-turn usage; this wrapper intercepts every
 * [chatCompletion] call, sums the three OpenAI token keys
 * (`prompt_tokens` / `completion_tokens` / `total_tokens`), and exposes the
 * running totals via [usage]. The Kotlin [LlmResponse.usage] field uses
 * `inputTokens` / `outputTokens` / `totalTokens` naming, so the mapping is
 * `inputTokens → prompt_tokens`, `outputTokens → completion_tokens`,
 * `totalTokens → total_tokens`.
 */
private class UsageChat(private val inner: LlmClient) : LlmClient {
    val usage: MutableMap<String, Int> = LinkedHashMap()

    init {
        usage[USAGE_PROMPT] = 0
        usage[USAGE_COMPLETION] = 0
        usage[USAGE_TOTAL] = 0
    }

    override suspend fun chatCompletion(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): com.l2dchat.core.llm.LlmResponse {
        val response = inner.chatCompletion(messages, config)
        val u = response.usage
        if (u != null) {
            usage[USAGE_PROMPT] = usage.getValue(USAGE_PROMPT) + u.inputTokens
            usage[USAGE_COMPLETION] = usage.getValue(USAGE_COMPLETION) + u.outputTokens
            usage[USAGE_TOTAL] = usage.getValue(USAGE_TOTAL) + u.totalTokens
        }
        return response
    }

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): kotlinx.coroutines.flow.Flow<com.l2dchat.core.llm.LlmStreamEvent> =
        inner.chatCompletionStream(messages, config)

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: com.l2dchat.core.llm.LlmToolExecutor,
    ): com.l2dchat.core.llm.LlmResponse =
        inner.chatCompletionWithTools(messages, tools, config, toolExecutor)

    private companion object {
        const val USAGE_PROMPT: String = "prompt_tokens"
        const val USAGE_COMPLETION: String = "completion_tokens"
        const val USAGE_TOTAL: String = "total_tokens"
    }
}

/* ---------------------------------------------------------------------- */
/* Payload serialization (mirrors LtmTools.kt's private serializers)      */
/* ---------------------------------------------------------------------- */

/** Serialize a [NodeSummary] to the progressive-disclosure payload shape. */
private fun NodeSummary.toSummaryMap(): Map<String, Any?> = mapOf(
    "hash" to hash,
    "preview" to preview,
    "mention_time" to mentionTime,
    "entities" to entities,
    "node_type" to nodeType,
    "event_time" to eventTime,
)

/** Serialize an [MemRetrieval.ExpandedNode] to the expand payload shape. */
private fun MemRetrieval.ExpandedNode.toExpandedMap(): Map<String, Any?> = mapOf(
    "node" to node.toRow(),
    "entities" to entities.map { it.toEntityLinkMap() },
    "events" to events.map { it.toEventLinkMap() },
    "supersede_chain" to supersedeChain,
    "predecessors" to predecessors,
)

/** Serialize an [MemRetrieval.EntityLinkView] to a plain map. */
private fun MemRetrieval.EntityLinkView.toEntityLinkMap(): Map<String, Any?> = mapOf(
    "hash" to hash,
    "canonical_name" to canonicalName,
    "role" to role,
    "mention_count" to mentionCount,
)

/** Serialize an [MemRetrieval.EventLinkView] to a plain map. */
private fun MemRetrieval.EventLinkView.toEventLinkMap(): Map<String, Any?> = mapOf(
    "hash" to hash,
    "role" to role,
    "mention_count" to mentionCount,
    "mention_time" to mentionTime,
    "preview" to preview,
)
