package com.l2dchat.core.mem

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * Host planner tool surface — Kotlin port of `mem/planner.py::build_planner_tools`
 * (design section 6).
 *
 * [buildLtmTools] assembles the ≤4-tool face a host planner (e.g. the Maimchat
 * main agent) sees. Everything here is a composition of the section-8 atoms via
 * [MemRetrieval] / [MemStore] / [ExtractionPipeline] — no new storage path —
 * and every handler reports failure in its result payload, never by raising:
 * the host's agent loop is a foreign system whose context and budget should
 * not burn on exceptions.
 *
 * Progressive disclosure: `ltm_search` returns summaries only; full text
 * requires `ltm_expand`; media requires `ltm_read_media`; multi-hop research
 * is delegated to the search sub agent via `ltm_ask` (present only when
 * [buildLtmTools] is given a non-null [MemSearchAgent]).
 *
 * ## `Long` epoch seconds
 *
 * All time fields are epoch seconds as [Long] (never [Double]/[Float]), per
 * the port-wide convention. The Python reference uses `float` and
 * `datetime.fromtimestamp`; [nowFn] returns [Long] and [ltmStore] formats the
 * idempotency suffix via [DateTimeFormatter] on the [ZoneId].
 *
 * @see buildLtmTools
 * @see LtmTool
 */

/**
 * One host-facing tool definition.
 *
 * Mirrors the Python `react.ToolDef` shape: an OpenAI function-tool schema
 * (split into [name], [description], [parametersJsonSchema] for easy adapter
 * wiring) plus a suspend [handler] that turns a arguments map into a result
 * map. The handler **never throws** — every failure path is encoded as a
 * result-map entry (typically `{"error": "..."}`) so the host planner's loop
 * budget is not burned on exceptions.
 *
 * The phase-6b runtime layer will adapt each [LtmTool] into the app's concrete
 * `Tool` interface; this file stays pure mem (no `ToolRegistry` dependency).
 *
 * @property name Tool name as it appears in the OpenAI function schema.
 * @property description Human/LLM-readable description (the function schema's
 *   `description` field).
 * @property parametersJsonSchema The `parameters` object of the OpenAI
 *   function-tool schema (a JSON-Schema object with `type`, `properties`,
 *   `required`, `additionalProperties`). Verbatim from `planner.py`.
 * @property handler Suspends, takes a `Map<String, Any?>` of arguments, returns
 *   a `Map<String, Any?>` payload. Unknown keys, bad types, and missing data
 *   all become `{"error": "..."}` payloads — never thrown exceptions.
 */
data class LtmTool(
    val name: String,
    val description: String,
    val parametersJsonSchema: Map<String, Any?>,
    val handler: suspend (arguments: Map<String, Any?>) -> Map<String, Any?>,
)

/**
 * Assemble the host planner's memory tool surface (design section 6).
 *
 * Returns a name-keyed map of [LtmTool]s. The three resident tools
 * (`ltm_search`, `ltm_expand`, `ltm_read_media`) are always present;
 * `ltm_ask` is added only when [searchAgent] is non-null (design: the search
 * sub agent is an optional dependency — when absent the planner only has
 * one-shot lookups); `ltm_store` is added only when [pipeline] is non-null
 * **and** [enableStore] is `true` (design: manual recording is opt-in).
 *
 * @param store The memory store; `ltm_read_media` reads nodes directly via R1,
 *   and `ltm_expand` / `ltm_search` resolve prefixes via R8.
 * @param retrieval The retrieval facade (search / temporal / expand). Built
 *   by the caller so the same `nowFn` / `zoneId` flow into the rank math.
 * @param pipeline The extraction pipeline for `ltm_store`. Required (together
 *   with [enableStore]) for that tool to appear.
 * @param searchAgent The [MemSearchAgent] for `ltm_ask`. When `null` the tool
 *   is absent and the planner only has one-shot lookups. Mirrors Python's
 *   `build_planner_tools(search_agent=...)`.
 * @param enableStore Master switch for `ltm_store`.
 * @param nowFn Clock for `ltm_store`'s anchor/idempotency key. Returns epoch
 *   seconds as [Long].
 * @param zoneId Timezone used to render the `manual_<hash>_<YYYYMMDD>`
 *   idempotency suffix. Defaults to [ZoneId.systemDefault] to mirror Python's
 *   `datetime.fromtimestamp`.
 * @return Tool name -> [LtmTool]. Always includes `ltm_search`,
 *   `ltm_expand`, `ltm_read_media`; includes `ltm_ask` when [searchAgent] is
 *   non-null; includes `ltm_store` when both [pipeline] is non-null and
 *   [enableStore] is `true`.
 */
fun buildLtmTools(
    store: MemStore,
    retrieval: MemRetrieval,
    pipeline: ExtractionPipeline? = null,
    searchAgent: MemSearchAgent? = null,
    enableStore: Boolean = false,
    nowFn: () -> Long = { System.currentTimeMillis() / 1000L },
    zoneId: ZoneId = ZoneId.systemDefault(),
): Map<String, LtmTool> {
    val tools: MutableMap<String, LtmTool> = LinkedHashMap()

    tools["ltm_search"] = LtmTool(
        name = "ltm_search",
        description = LTM_SEARCH_DESCRIPTION,
        parametersJsonSchema = ltmSearchParameters(),
        handler = { args -> ltmSearch(retrieval, args) },
    )

    tools["ltm_expand"] = LtmTool(
        name = "ltm_expand",
        description = LTM_EXPAND_DESCRIPTION,
        parametersJsonSchema = ltmExpandParameters(),
        handler = { args -> ltmExpand(retrieval, store, args) },
    )

    tools["ltm_read_media"] = LtmTool(
        name = "ltm_read_media",
        description = LTM_READ_MEDIA_DESCRIPTION,
        parametersJsonSchema = ltmReadMediaParameters(),
        handler = { args -> ltmReadMedia(store, args) },
    )

    if (searchAgent != null) {
        tools["ltm_ask"] = LtmTool(
            name = "ltm_ask",
            description = LTM_ASK_DESCRIPTION,
            parametersJsonSchema = ltmAskParameters(),
            handler = { args -> ltmAsk(searchAgent, args) },
        )
    }

    if (pipeline != null && enableStore) {
        tools["ltm_store"] = LtmTool(
            name = "ltm_store",
            description = LTM_STORE_DESCRIPTION,
            parametersJsonSchema = ltmStoreParameters(),
            handler = { args -> ltmStore(pipeline, args, nowFn, zoneId) },
        )
    }

    return tools
}

/* ---------------------------------------------------------------------- */
/* ltm_search                                                             */
/* ---------------------------------------------------------------------- */

/**
 * Dual-mode search: semantic (with query) or temporal (time_range only).
 *
 * Mirrors `planner.py::ltm_search`. With a non-blank `query`, delegates to
 * [MemRetrieval.search] (time_range / entity / node_type become filters);
 * without a query but with a non-blank `time_range`, delegates to
 * [MemRetrieval.temporal] (pure axis scan). Neither → error payload.
 *
 * `k` is validated to an integer in `[1, 20]`; out-of-range or non-integer
 * values yield an error payload (no throw). Unexpected keys yield an error
 * payload. A bad `time_axis` or unparseable `time_range` raised by the
 * retrieval layer is caught and turned into an error payload.
 */
private suspend fun ltmSearch(
    retrieval: MemRetrieval,
    args: Map<String, Any?>,
): Map<String, Any?> {
    val extra = unexpectedKeys(args, LTM_SEARCH_KNOWN_KEYS)
    if (extra != null) {
        return mapOf("results" to emptyList<Any>(), "error" to extra)
    }

    val kValue = checkedK(args["k"])
    val kError = kValue.second
    if (kError != null) {
        return mapOf("results" to emptyList<Any>(), "error" to kError)
    }
    val k = kValue.first!!

    val query = args["query"] as? String
    val timeRange = args["time_range"] as? String
    val hasQuery = !query.isNullOrBlank()
    val hasRange = !timeRange.isNullOrBlank()

    if (!hasQuery && !hasRange) {
        return mapOf(
            "results" to emptyList<Any>(),
            "error" to "provide query or time_range (or both)",
        )
    }

    val timeAxis = (args["time_axis"] as? String).orEmpty().ifBlank { "event" }
    val entity = args["entity"] as? String
    val nodeType = args["node_type"] as? String

    val results: List<NodeSummary> = try {
        if (hasQuery) {
            retrieval.search(
                query = query!!.trim(),
                k = k,
                nodeType = nodeType?.takeIf { it.isNotBlank() },
                timeRange = timeRange?.takeIf { it.isNotBlank() },
                timeAxis = timeAxis,
                entity = entity?.takeIf { it.isNotBlank() },
            )
        } else {
            retrieval.temporal(
                timeRange = timeRange!!.trim(),
                axis = timeAxis,
                k = k,
                nodeType = nodeType?.takeIf { it.isNotBlank() },
            )
        }
    } catch (e: ToolValidationError) {
        return mapOf("results" to emptyList<Any>(), "error" to (e.message ?: "validation error"))
    }

    return mapOf("results" to results.map { it.toMap() }, "error" to null)
}

/* ---------------------------------------------------------------------- */
/* ltm_expand                                                             */
/* ---------------------------------------------------------------------- */

/**
 * Full expand of one node by full hash or ≥6-char prefix.
 *
 * Mirrors `planner.py::ltm_expand`. Prefix resolution goes through
 * [MemStore.resolveHash]; an ambiguous prefix yields an error payload with the
 * candidate list, an unknown prefix yields an error payload, and a missing
 * node (resolved hash but no row) yields an error payload. On success returns
 * the full [MemRetrieval.ExpandedNode] serialized as a map.
 */
private suspend fun ltmExpand(
    retrieval: MemRetrieval,
    store: MemStore,
    args: Map<String, Any?>,
): Map<String, Any?> {
    val extra = unexpectedKeys(args, setOf("hash"))
    if (extra != null) {
        return mapOf("node" to null, "error" to extra)
    }

    val (fullHash, resolveError) = resolveHashArg(store, args["hash"])
    if (resolveError != null) {
        return mapOf("node" to null, "error" to resolveError)
    }

    val expanded = retrieval.expand(fullHash!!)
    if (expanded == null) {
        return mapOf("node" to null, "error" to "node not found: $fullHash")
    }
    return mapOf("node" to expanded.toMap(), "error" to null)
}

/* ---------------------------------------------------------------------- */
/* ltm_read_media                                                         */
/* ---------------------------------------------------------------------- */

/**
 * Media reference lookup (host reads the bytes).
 *
 * Mirrors `planner.py::ltm_read_media`. Resolves the hash/prefix via R8, then
 * reads the node directly via R1 (`includeSuperseded = true`, matching Python's
 * `get_node(..., include_superseded=True)`) and projects `media_path`,
 * `modality`, `captured_at` out of the node's metadata. A node without a
 * `media_path` yields an error payload. **Does not record access** — the
 * Python reference deliberately bypasses `expand` here so reading a media
 * reference does not inflate `access_count`.
 */
private suspend fun ltmReadMedia(
    store: MemStore,
    args: Map<String, Any?>,
): Map<String, Any?> {
    val extra = unexpectedKeys(args, setOf("hash"))
    if (extra != null) {
        return mapOf("media_path" to null, "error" to extra)
    }

    val (fullHash, resolveError) = resolveHashArg(store, args["hash"])
    if (resolveError != null) {
        return mapOf("media_path" to null, "error" to resolveError)
    }

    val node = store.getNode(fullHash!!, includeSuperseded = true)
    if (node == null) {
        return mapOf("media_path" to null, "error" to "node not found: $fullHash")
    }

    val metadata = node.metadata
    val mediaPath = metadata["media_path"] as? String
    if (mediaPath.isNullOrBlank()) {
        return mapOf(
            "media_path" to null,
            "error" to "node ${fullHash.take(8)} carries no media_path",
        )
    }

    return mapOf(
        "media_path" to mediaPath,
        "modality" to metadata["modality"],
        "captured_at" to metadata["captured_at"],
        "error" to null,
    )
}

/* ---------------------------------------------------------------------- */
/* ltm_store                                                              */
/* ---------------------------------------------------------------------- */

/**
 * Manual ingest entry — same extraction pipeline as conversation compacts.
 *
 * Mirrors `planner.py::ltm_store`. Builds an [IngestBatch] with
 * `batch_id = "manual_" + short_hash(text) + "_" + YYYYMMDD`,
 * `source = MANUAL`, `time_anchor = mention_time = nowFn()`,
 * `metadata = {"entry": "ltm_store"}`, then runs [ExtractionPipeline.ingest].
 *
 * Idempotency (design 6.2): same text same day replays the receipt unchanged
 * (the pipeline's own `done`-receipt short-circuit means the LLM is not
 * invoked a second time); cross-day re-stores re-run extraction but content
 * addressing keeps nodes unique.
 */
private suspend fun ltmStore(
    pipeline: ExtractionPipeline,
    args: Map<String, Any?>,
    nowFn: () -> Long,
    zoneId: ZoneId,
): Map<String, Any?> {
    val extra = unexpectedKeys(args, setOf("text"))
    if (extra != null) {
        return mapOf("batch_id" to null, "status" to null, "error" to extra)
    }

    val text = args["text"] as? String
    if (text.isNullOrBlank()) {
        return mapOf(
            "batch_id" to null,
            "status" to null,
            "error" to "text: expected a non-empty string",
        )
    }

    val nowEpoch = nowFn()
    val nowLocal = Instant.ofEpochSecond(nowEpoch).atZone(zoneId).toLocalDate()
    val yyyymmdd = nowLocal.format(YYYYMMDD_FORMATTER)
    val batchId = "manual_${Hashing.shortHash(text)}_$yyyymmdd"

    val batch = IngestBatch(
        batchId = batchId,
        source = BatchSource.MANUAL,
        text = text.trim(),
        timeAnchor = nowEpoch,
        mentionTime = nowEpoch,
        metadata = mapOf("entry" to "ltm_store"),
    )

    val receipt = pipeline.ingest(batch)
    return mapOf(
        "batch_id" to receipt.batchId,
        "status" to receipt.status.wireValue,
        "node_count" to receipt.nodeHashes.size,
        "link_count" to receipt.linkCount,
        "error" to receipt.error,
    )
}

/* ---------------------------------------------------------------------- */
/* ltm_ask                                                                */
/* ---------------------------------------------------------------------- */

/**
 * Delegate a complex memory question to the search sub agent.
 *
 * Mirrors `planner.py::ltm_ask`. Validates that `question` is a non-blank
 * string (unexpected keys or a missing/blank question yield an error payload
 * — never thrown), then forwards to [MemSearchAgent.ask] and returns its
 * `{"answer", "error", "stats"}` payload verbatim. The search agent itself
 * never throws (loop aborts and provider failures come back in `error`), so
 * this handler is total: every input produces a payload.
 */
private suspend fun ltmAsk(
    searchAgent: MemSearchAgent,
    args: Map<String, Any?>,
): Map<String, Any?> {
    val extra = unexpectedKeys(args, setOf("question"))
    if (extra != null) {
        return mapOf("answer" to "", "error" to extra)
    }

    val question = args["question"] as? String
    if (question.isNullOrBlank()) {
        return mapOf("answer" to "", "error" to "question: expected a non-empty string")
    }

    return searchAgent.ask(question.trim()).toDict()
}

/* ---------------------------------------------------------------------- */
/* Shared helpers                                                         */
/* ---------------------------------------------------------------------- */

/**
 * Validate the `k` argument (default 5, range `[1, 20]`).
 *
 * Mirrors `planner.py::_checked_k`. Booleans are rejected even though they
 * serialize as integers in some JSON libraries (Python's `isinstance(k, bool)`
 * guard). Non-integers and out-of-range values yield a descriptive error.
 */
private fun checkedK(raw: Any?): Pair<Int?, String?> {
    if (raw == null) return 5 to null
    if (raw is Boolean) return null to kErrorMessage(raw)
    val k = when (raw) {
        is Int -> raw
        is Long -> raw.toInt()
        is Number -> raw.toInt()
        else -> return null to kErrorMessage(raw)
    }
    if (k !in K_MIN..K_MAX) return null to kErrorMessage(raw)
    return k to null
}

/** Error string for a bad `k`, matching `planner.py`'s phrasing. */
private fun kErrorMessage(raw: Any?): String =
    "k: expected an integer in [$K_MIN, $K_MAX], got $raw"

/**
 * Resolve a hash/prefix argument to a single full hash.
 *
 * Mirrors `planner.py::_resolve`. Rejects non-strings and prefixes shorter
 * than [MIN_PREFIX] characters; asks [MemStore.resolveHash] for the matches
 * and errors on zero (unknown) or more than one (ambiguous, with the first
 * five candidates listed).
 *
 * @return `(fullHash, error)` — exactly one is non-null.
 */
private suspend fun resolveHashArg(store: MemStore, raw: Any?): Pair<String?, String?> {
    if (raw !is String) {
        return null to "hash: expected a full hash or ≥$MIN_PREFIX-char prefix"
    }
    val prefix = raw.trim()
    if (prefix.length < MIN_PREFIX) {
        return null to "hash: expected a full hash or ≥$MIN_PREFIX-char prefix"
    }
    val matches = store.resolveHash(prefix)
    if (matches.isEmpty()) {
        return null to "no node matches hash prefix '$prefix'"
    }
    if (matches.size > 1) {
        val preview = matches.take(5)
        return null to "hash prefix '$prefix' is ambiguous " +
            "(${matches.size} candidates: $preview) — give more characters"
    }
    return matches[0] to null
}

/**
 * Return an "unexpected arguments" error string for any keys in [args] that are
 * not in [known], or `null` when every key is known.
 *
 * Mirrors `planner.py::_unexpected`. The keys are sorted for deterministic
 * output (matching Python's `sorted(extra)`).
 */
private fun unexpectedKeys(args: Map<String, Any?>, known: Set<String>): String? {
    val extra = args.keys.filter { it !in known }.sorted()
    if (extra.isEmpty()) return null
    return "unexpected arguments: $extra"
}

/* ---------------------------------------------------------------------- */
/* Payload serialization                                                  */
/* ---------------------------------------------------------------------- */

/**
 * Serialize a [NodeSummary] to the progressive-disclosure payload shape.
 *
 * Mirrors the Python `present` output dict (design section 6.1 / 8.3). Key
 * names match the Python reference exactly so the host planner's prompt
 * examples stay valid.
 */
private fun NodeSummary.toMap(): Map<String, Any?> = mapOf(
    "hash" to hash,
    "preview" to preview,
    "mention_time" to mentionTime,
    "entities" to entities,
    "node_type" to nodeType,
    "event_time" to eventTime,
)

/**
 * Serialize an [MemRetrieval.ExpandedNode] to the expand payload shape.
 *
 * Mirrors the Python `expand` return dict. The node row is flattened to its
 * column dict via [Node.toRow] (so the host sees the same shape the store
 * persists), and the link views are projected to plain maps.
 */
private fun MemRetrieval.ExpandedNode.toMap(): Map<String, Any?> = mapOf(
    "node" to node.toRow(),
    "entities" to entities.map { it.toMap() },
    "events" to events.map { it.toMap() },
    "supersede_chain" to supersedeChain,
    "predecessors" to predecessors,
)

/** Serialize an [MemRetrieval.EntityLinkView] to a plain map. */
private fun MemRetrieval.EntityLinkView.toMap(): Map<String, Any?> = mapOf(
    "hash" to hash,
    "canonical_name" to canonicalName,
    "role" to role,
    "mention_count" to mentionCount,
)

/** Serialize an [MemRetrieval.EventLinkView] to a plain map. */
private fun MemRetrieval.EventLinkView.toMap(): Map<String, Any?> = mapOf(
    "hash" to hash,
    "role" to role,
    "mention_count" to mentionCount,
    "mention_time" to mentionTime,
    "preview" to preview,
)

/* ---------------------------------------------------------------------- */
/* OpenAI function-tool schemas (verbatim from planner.py)                */
/* ---------------------------------------------------------------------- */

/** `ltm_search` description, verbatim from `planner.py`. */
private const val LTM_SEARCH_DESCRIPTION: String =
    "Search long-term memory. Returns summaries (hash prefix, " +
        "80-char preview, times, entity names) — call ltm_expand for " +
        "full text. With query: semantic search (time_range/entity/" +
        "node_type filter it). Without query: time-axis scan of " +
        "time_range. For complex multi-hop questions, prefer ltm_ask."

/** `ltm_expand` description, verbatim from `planner.py`. */
private const val LTM_EXPAND_DESCRIPTION: String =
    "Full content + metadata + linked entities + supersede chain " +
        "of one memory node, by full hash or the 8-char prefix shown " +
        "in ltm_search summaries."

/** `ltm_read_media` description, verbatim from `planner.py`. */
private const val LTM_READ_MEDIA_DESCRIPTION: String =
    "Get the media reference (host-side file path, modality, " +
        "capture time) attached to a memory node. Reading the file " +
        "itself is up to you."

/** `ltm_store` description, verbatim from `planner.py`. */
private const val LTM_STORE_DESCRIPTION: String =
    "Record a piece of text into long-term memory right now " +
        "(manual entry). Goes through the same extraction pipeline " +
        "as conversation compacts."

/** `ltm_ask` description, verbatim from `planner.py`. */
private const val LTM_ASK_DESCRIPTION: String =
    "Delegate a complex memory question to the memory search " +
        "agent: it runs multi-round searches internally and returns " +
        "a grounded answer with hash citations. Use for multi-hop or " +
        "vague questions; for a simple lookup prefer ltm_search."

/** Known argument keys for `ltm_search` (for unexpected-arg validation). */
private val LTM_SEARCH_KNOWN_KEYS: Set<String> = setOf(
    "query", "k", "time_range", "time_axis", "entity", "node_type",
)

/** `ltm_search` parameters schema, verbatim from `planner.py`. */
private fun ltmSearchParameters(): Map<String, Any?> = mapOf(
    "type" to "object",
    "properties" to mapOf(
        "query" to mapOf(
            "type" to "string",
            "description" to "free-text query; omit for a pure time scan",
        ),
        "k" to mapOf(
            "type" to "integer",
            "description" to "max results, 1-20 (default 5)",
        ),
        "time_range" to mapOf(
            "type" to "string",
            "description" to "normalized time filter, e.g. 2026-07 or a/b interval",
        ),
        "time_axis" to mapOf(
            "type" to "string",
            "enum" to listOf("event", "mention"),
            "description" to "event = happened then, mention = recorded then",
        ),
        "entity" to mapOf(
            "type" to "string",
            "description" to "restrict to nodes linked to this entity",
        ),
        "node_type" to mapOf(
            "type" to "string",
            "description" to "exact node_type filter (event/life_capture/…)",
        ),
    ),
    "required" to emptyList<String>(),
    "additionalProperties" to false,
)

/** `ltm_expand` parameters schema, verbatim from `planner.py`. */
private fun ltmExpandParameters(): Map<String, Any?> = mapOf(
    "type" to "object",
    "properties" to mapOf(
        "hash" to mapOf(
            "type" to "string",
            "description" to "full hash or ≥6-char prefix",
        ),
    ),
    "required" to listOf("hash"),
    "additionalProperties" to false,
)

/** `ltm_read_media` parameters schema, verbatim from `planner.py`. */
private fun ltmReadMediaParameters(): Map<String, Any?> = mapOf(
    "type" to "object",
    "properties" to mapOf(
        "hash" to mapOf(
            "type" to "string",
            "description" to "full hash or ≥6-char prefix",
        ),
    ),
    "required" to listOf("hash"),
    "additionalProperties" to false,
)

/** `ltm_store` parameters schema, verbatim from `planner.py`. */
private fun ltmStoreParameters(): Map<String, Any?> = mapOf(
    "type" to "object",
    "properties" to mapOf(
        "text" to mapOf(
            "type" to "string",
            "description" to "the text to remember",
        ),
    ),
    "required" to listOf("text"),
    "additionalProperties" to false,
)

/** `ltm_ask` parameters schema, verbatim from `planner.py`. */
private fun ltmAskParameters(): Map<String, Any?> = mapOf(
    "type" to "object",
    "properties" to mapOf(
        "question" to mapOf(
            "type" to "string",
            "description" to "natural-language question",
        ),
    ),
    "required" to listOf("question"),
    "additionalProperties" to false,
)

/* ---------------------------------------------------------------------- */
/* Constants                                                              */
/* ---------------------------------------------------------------------- */

/**
 * Hash prefixes below this length are not serviced (see [MemStore.resolveHash]
 * and `planner.py::_MIN_PREFIX`).
 */
private const val MIN_PREFIX: Int = 6

/** `ltm_search` result-count bounds (planner context budget). */
private const val K_MIN: Int = 1

/** `ltm_search` result-count bounds (planner context budget). */
private const val K_MAX: Int = 20

/**
 * Formatter for the `manual_<hash>_<YYYYMMDD>` idempotency suffix.
 *
 * Mirrors Python's `datetime.fromtimestamp(now_fn()).strftime("%Y%m%d")`. Uses
 * [Locale.ROOT] so month/day names (never emitted by this pattern) are stable
 * across devices; the pattern itself is pure digits.
 */
private val YYYYMMDD_FORMATTER: DateTimeFormatter =
    DateTimeFormatter.ofPattern("yyyyMMdd", Locale.ROOT)
