package com.l2dchat.core.mem

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmResponse
import java.time.ZoneId
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Extraction pipeline: [IngestBatch] → bounded agent loop → one transaction.
 *
 * Kotlin port of `mem/extract.py::ExtractionPipeline`. The design-section-2
 * flow, end to end:
 *
 * 1. **Idempotency**: a `done` receipt for [IngestBatch.batchId] short-circuits
 *    the ingest and returns the old receipt; a `failed` one does not block a
 *    retry.
 * 2. A fresh [TxWriter] is staged by the 8 agent tools while a bounded
 *    [ReActLoop] drives the extraction model over the batch text.
 * 3. **Closing validation** (code, not the model): every content node's
 *    declared `entity_mentions` must be covered by links staged (or already
 *    stored) for that node. Misses are fed back as a user message and the loop
 *    continues on the same conversation, at most [maxValidationRetries] times.
 * 4. **Success**: the receipt is staged into the same staging area and
 *    everything lands in one transaction.
 * 5. **Any failure** (loop abort, validation retries exhausted, flush error,
 *    provider error): the staging area is discarded untouched and a `failed`
 *    receipt is persisted in its own small transaction. [ingest] never raises;
 *    the caller checks [Receipt.status].
 *
 * ## `Long` epoch seconds
 *
 * The Python reference carries `mention_time` as a `float` epoch. This port
 * keeps the whole chain in [Long] epoch seconds (per the port-wide
 * convention), so [mentionTimeEpoch] is a [Long].
 *
 * @property store The memory store (read atoms + [MemStore.apply]).
 * @property llm Chat client for the extraction agent (real or a fake).
 * @property config Generation config forwarded to every LLM call.
 * @property embedder Embedding client (`dimension` + `embed`).
 * @property registry Node type registry.
 * @property maxTurns [ReActLoop] turn budget per run.
 * @property maxValidationRetries Closing-validation feedback rounds before the
 *   batch is marked failed.
 * @property llmMm Optional multimodal chat client used instead of [llm] for
 *   batches that carry image attachments (some extraction endpoints are
 *   text-only while a sibling model accepts images).
 * @property configMm Optional generation config for the multimodal client.
 * @property zoneId Timezone used by [TimeParse] when expanding `event_time`
 *   strings during closing validation.
 */
class ExtractionPipeline(
    private val store: MemStore,
    private val llm: LlmClient,
    private val config: LlmGenerationConfig,
    private val embedder: EmbeddingClient,
    private val registry: NodeTypeRegistry,
    private val maxTurns: Int = DEFAULT_MAX_TURNS,
    private val maxValidationRetries: Int = DEFAULT_MAX_VALIDATION_RETRIES,
    private val llmMm: LlmClient? = null,
    private val configMm: LlmGenerationConfig? = null,
    private val zoneId: ZoneId = ZoneId.systemDefault(),
    private val promptExtra: String? = null,
) {
    init {
        require(maxTurns >= 1) { "ExtractionPipeline: maxTurns must be >= 1, got $maxTurns" }
        require(maxValidationRetries >= 0) {
            "ExtractionPipeline: maxValidationRetries must be >= 0, got $maxValidationRetries"
        }
    }

    /**
     * Per-batchId serialization locks.
     *
     * Concurrent `ingest` calls with the SAME [IngestBatch.batchId] serialize on the same
     * [Mutex] so the second caller observes the `done` receipt written by the first and
     * short-circuits — preventing duplicate LLM cost and a double `mention_count` bump from
     * the narrow window between the idempotency check and the receipt write. Ingests of
     * DIFFERENT batchIds acquire different mutexes and stay parallel.
     *
     * The map is a [ConcurrentHashMap] so `computeIfAbsent` is atomic; entries are tiny
     * (one [Mutex] each) and bounded by the distinct batchId space (compact spans +
     * life_capture + manual), so no eviction is needed.
     */
    private val batchLocks: ConcurrentHashMap<String, Mutex> = ConcurrentHashMap()

    /**
     * Ingest one batch; never raises — the receipt carries the outcome.
     *
     * @param batch The batch to ingest.
     * @return A `done` receipt with the written node hashes on success, a
     *   `failed` receipt with the error reason otherwise. A batch whose receipt
     *   is already `done` is skipped idempotently and the old receipt returned.
     */
    suspend fun ingest(batch: IngestBatch): Receipt {
        // Serialize same-batchId ingests: the second caller must observe the `done` receipt
        // written by the first and short-circuit, rather than both passing the idempotency
        // check and running the full agent loop (duplicate LLM cost + double mention_count).
        // Different batchIds acquire different mutexes and run in parallel.
        val lock = batchLocks.computeIfAbsent(batch.batchId) { Mutex() }
        return lock.withLock { ingestLocked(batch) }
    }

    private suspend fun ingestLocked(batch: IngestBatch): Receipt {
        val existing = store.getReceipt(batch.batchId)
        if (existing != null && existing.status == ReceiptStatus.DONE) {
            return existing
        }

        val tx = TxWriter(dimensions = embedder.dimension)
        val multimodal = batch.images.isNotEmpty()
        val activeLlm: LlmClient = if (multimodal && llmMm != null) llmMm else llm
        val activeConfig: LlmGenerationConfig =
            if (multimodal && configMm != null) configMm else config
        val client = LoopClient(activeLlm)
        val tools =
            AgentTools.buildAgentTools(
                store = store,
                tx = tx,
                embedder = embedder,
                registry = registry,
                mentionTimeEpoch = batch.mentionTime,
                batchMetadata = batch.metadata,
            )
        val loop = ReActLoop(client, tools, activeConfig, maxTurns = maxTurns)
        val systemPrompt =
            Prompts.buildSystemPrompt(
                registry = registry,
                timeAnchorEpoch = batch.timeAnchor,
                zoneId = zoneId,
                extra = promptExtra,
            )

        val calls: MutableList<ToolCallRecord> = ArrayList()
        var turns = 0
        var retries = 0
        var error: String? = null
        var receipt: Receipt? = null

        try {
            // Built inside the try: an unreadable/oversized image attachment
            // raises ConfigError, which must become a failed receipt, not
            // escape ingest (it never raises).
            val userContent = Multimodal.buildUserContent(batch)
            var result = loop.run(systemPrompt, userContent)
            turns += result.turns
            calls.addAll(result.calls)
            var missing = missingEntityLinks(calls)
            while (missing.isNotEmpty() && retries < maxValidationRetries) {
                retries += 1
                client.continueWith(feedbackMessage(missing), result.finalContent)
                result = loop.run(systemPrompt, userContent)
                turns += result.turns
                calls.addAll(result.calls)
                missing = missingEntityLinks(calls)
            }
            if (missing.isNotEmpty()) {
                throw ClosingValidationError(
                    "closing validation failed after $retries validation retries: " +
                        "unlinked entity mentions: $missing"
                )
            }
            val (nodeHashes, linkCount) = writtenNodesAndLinks(calls)
            val done =
                Receipt(
                    batchId = batch.batchId,
                    source = batch.source.wireValue,
                    status = ReceiptStatus.DONE,
                    nodeHashes = nodeHashes,
                    linkCount = linkCount,
                    error = null,
                    createdAt = nowEpochSeconds(),
                )
            tx.putReceipt(
                TxOp.PutReceipt(
                    batchId = done.batchId,
                    source = done.source,
                    status = done.status.wireValue,
                    nodeHashes = done.nodeHashes,
                    linkCount = done.linkCount,
                    error = done.error,
                    createdAt = done.createdAt,
                )
            )
            tx.flush(store)
            receipt = done
        } catch (e: ReactLoopAbort) {
            calls.addAll(e.calls.filterIsInstance<ToolCallRecord>())
            error = e.message ?: "react loop aborted"
        } catch (e: Throwable) {
            // Closing validation, flush, provider errors — all become a failed
            // receipt. CancellationException is re-thrown to honor structured
            // concurrency (it must not be swallowed).
            if (e is kotlinx.coroutines.CancellationException) throw e
            error = e.message ?: e.javaClass.simpleName
        }

        val stats: Map<String, Any?> =
            mapOf(
                "turns" to turns,
                "tool_calls" to calls.size,
                "validation_retries" to retries,
                "usage" to client.usage,
            )

        if (error != null) {
            val failed =
                Receipt(
                    batchId = batch.batchId,
                    source = batch.source.wireValue,
                    status = ReceiptStatus.FAILED,
                    nodeHashes = emptyList(),
                    linkCount = 0,
                    error = error,
                    createdAt = nowEpochSeconds(),
                    stats = stats,
                )
            persistFailedReceipt(failed)
            return failed
        }

        return receipt!!.copy(stats = stats)
    }

    // ------------------------------------------------------------------
    // Closing validation (design section 2, check b)
    // ------------------------------------------------------------------

    /**
     * Map each staged content node to its `entity_mentions` not yet linked.
     *
     * Computed from the loop's tool-call audit log (valid calls only) plus the
     * store's read atoms: [TxWriter] deliberately exposes no staging readback,
     * and the audit log is the same source of truth the loop acted on. A
     * mention is covered when any entity it resolves to (stored entities via
     * exact canonical/alias match, or entities registered in this run by
     * name/alias) is linked to the content node — by a staged link or an
     * already-stored one.
     *
     * @return A map from content hash to the list of unlinked mention names.
     *   Empty when every content node's mentions are fully linked.
     */
    @Suppress("UNCHECKED_CAST")
    private suspend fun missingEntityLinks(calls: List<ToolCallRecord>): Map<String, List<String>> {
        val contentMentions: MutableMap<String, MutableList<String>> = LinkedHashMap()
        val runEntityNames: MutableMap<String, MutableSet<String>> = LinkedHashMap()
        val stagedLinks: MutableMap<String, MutableSet<String>> = LinkedHashMap()

        for (call in calls) {
            if (!call.validationOk) continue
            val result = call.result as? Map<*, *> ?: continue
            val args = (call.arguments as? Map<*, *>) ?: emptyMap<String, Any?>()
            when (call.name) {
                "register_content" -> {
                    val hashId = result["hash"] as? String ?: continue
                    val bucket = contentMentions.getOrPut(hashId) { ArrayList() }
                    val mentions = args["entity_mentions"] as? List<*> ?: emptyList<Any?>()
                    for (mention in mentions) {
                        if (mention is String && mention !in bucket) {
                            bucket.add(mention)
                        }
                    }
                }
                "register_entity" -> {
                    val hashId = result["hash"] as? String ?: continue
                    val names = runEntityNames.getOrPut(hashId) { LinkedHashSet() }
                    (args["name"] as? String)?.let { names.add(it.trim().lowercase(Locale.ROOT)) }
                    (args["aliases"] as? List<*>)?.forEach { alias ->
                        if (alias is String) names.add(alias.trim().lowercase(Locale.ROOT))
                    }
                }
                "update_entity_aliases" -> {
                    val entityHash = args["entity_hash"] as? String ?: continue
                    val names = runEntityNames.getOrPut(entityHash) { LinkedHashSet() }
                    (args["add_aliases"] as? List<*>)?.forEach { alias ->
                        if (alias is String) names.add(alias.trim().lowercase(Locale.ROOT))
                    }
                }
                "link_event_entity" -> {
                    val contentHash = args["content_hash"] as? String ?: continue
                    val entityHash = args["entity_hash"] as? String ?: continue
                    stagedLinks.getOrPut(contentHash) { LinkedHashSet() }.add(entityHash)
                }
            }
        }

        val missing: MutableMap<String, MutableList<String>> = LinkedHashMap()
        for ((contentHash, mentions) in contentMentions) {
            val linked: MutableSet<String> = LinkedHashSet(stagedLinks[contentHash] ?: emptySet())
            for (link in store.getLinks(eventHash = contentHash)) {
                linked.add(link.entityHash)
            }
            for (mention in mentions) {
                val needle = mention.trim().lowercase(Locale.ROOT)
                val candidates: MutableSet<String> = LinkedHashSet()
                for (node in store.findEntities(mention)) {
                    candidates.add(node.hashId)
                }
                for ((hashId, names) in runEntityNames) {
                    if (needle in names) candidates.add(hashId)
                }
                if (linked.intersect(candidates).isEmpty()) {
                    missing.getOrPut(contentHash) { ArrayList() }.add(mention)
                }
            }
        }
        return missing
    }

    /**
     * Render the missing-link list as the retry-round user message.
     *
     * Mirrors `_feedback_message` in `extract.py`.
     */
    private fun feedbackMessage(missing: Map<String, List<String>>): String {
        val lines = ArrayList<String>()
        lines.add(
            "Closing validation failed: these content nodes mention entities " +
                "that are not linked to them."
        )
        lines.add(
            "For every name below, call link_event_entity(content_hash, " +
                "entity_hash) — registering the entity first with register_entity " +
                "if it does not exist yet. Do NOT re-register content that " +
                "already exists; only add the missing links."
        )
        for ((contentHash, names) in missing) {
            val joined = names.joinToString(", ") { "'$it'" }
            lines.add("- content $contentHash: unlinked mentions: $joined")
        }
        return lines.joinToString("\n")
    }

    // ------------------------------------------------------------------
    // Receipt helpers
    // ------------------------------------------------------------------

    /**
     * Collect the batch's node hashes and created-link count from the log.
     *
     * Mirrors `_written_nodes_and_links` in `extract.py`.
     */
    private fun writtenNodesAndLinks(calls: List<ToolCallRecord>): Pair<List<String>, Int> {
        val nodeHashes: MutableList<String> = ArrayList()
        var linkCount = 0
        for (call in calls) {
            if (!call.validationOk) continue
            val result = call.result as? Map<*, *> ?: continue
            when (call.name) {
                "register_entity", "register_content" -> {
                    val hashId = result["hash"] as? String ?: continue
                    if (hashId !in nodeHashes) nodeHashes.add(hashId)
                }
                "link_event_entity" -> {
                    val created = result["created"] as? Boolean ?: false
                    if (created) linkCount += 1
                }
            }
        }
        return nodeHashes to linkCount
    }

    /**
     * Write a failed receipt in its own small transaction (best effort).
     *
     * Mirrors `_persist_failed_receipt` in `extract.py`. The main staging area
     * is simply discarded, so the batch leaves no half-committed state behind;
     * if even this write fails, the caller still gets the in-memory failed
     * receipt.
     */
    private suspend fun persistFailedReceipt(receipt: Receipt) {
        try {
            val ftx = TxWriter(dimensions = embedder.dimension)
            ftx.putReceipt(
                TxOp.PutReceipt(
                    batchId = receipt.batchId,
                    source = receipt.source,
                    status = receipt.status.wireValue,
                    nodeHashes = receipt.nodeHashes,
                    linkCount = receipt.linkCount,
                    error = receipt.error,
                    createdAt = receipt.createdAt,
                )
            )
            ftx.flush(store)
        } catch (e: Throwable) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            // Best-effort: the caller already has the in-memory failed receipt.
        }
    }

    private fun nowEpochSeconds(): Long = System.currentTimeMillis() / 1000L

    companion object {
        /** Default turn budget per run, matching `extract.py`'s default. */
        const val DEFAULT_MAX_TURNS: Int = 12

        /** Default closing-validation retry rounds, matching `extract.py`'s default. */
        const val DEFAULT_MAX_VALIDATION_RETRIES: Int = 2
    }
}

/**
 * Raised when closing validation still fails after all retry rounds.
 *
 * Internal to the pipeline; caught and converted to a failed receipt.
 */
private class ClosingValidationError(message: String) : Exception(message)

/**
 * Chat-client bridge in front of the real LLM client — Kotlin port of
 * `mem/extract.py::_LoopClient`.
 *
 * Two jobs: accumulate token usage across every turn ([ReActLoop] discards
 * per-turn [LlmTokenUsage]), and let a completed run be CONTINUED:
 * [ReActLoop.run] always seeds a fresh (system, user) pair, so on a validation
 * retry the previous conversation plus the feedback message is spliced in
 * here, replacing that seed pair.
 *
 * The bridge wraps the [LlmClient] so the [ReActLoop] sees a single client
 * whose `chatCompletion` calls carry the accumulated conversation. This is
 * necessary because the Kotlin [LlmClient] interface takes a fresh message
 * list per call (no session state), while the Python `_LoopClient.chat` mutates
 * a shared `messages` list.
 */
private class LoopClient(private val inner: LlmClient) : LlmClient {
    val usage: MutableMap<String, Int> =
        mutableMapOf(
            "prompt_tokens" to 0,
            "completion_tokens" to 0,
            "total_tokens" to 0,
        )

    private var seed: List<LlmMessage>? = null
    private var lastMessages: List<LlmMessage> = emptyList()

    override suspend fun chatCompletion(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ): LlmResponse {
        val effectiveMessages: List<LlmMessage> =
            if (seed != null) {
                // messages always starts with the loop's fresh [system, user]
                // pair; replace it with the stored conversation.
                val tail = if (messages.size > 2) messages.subList(2, messages.size) else emptyList()
                seed!! + tail
            } else {
                messages
            }
        lastMessages = effectiveMessages
        val result = inner.chatCompletion(effectiveMessages, config)
        result.usage?.let { u ->
            usage["prompt_tokens"] = usage["prompt_tokens"]!! + u.inputTokens
            usage["completion_tokens"] = usage["completion_tokens"]!! + u.outputTokens
            usage["total_tokens"] = usage["total_tokens"]!! + u.totalTokens
        }
        return result
    }

    override fun chatCompletionStream(
        messages: List<LlmMessage>,
        config: LlmGenerationConfig,
    ) = inner.chatCompletionStream(messages, config)

    override suspend fun chatCompletionWithTools(
        messages: List<LlmMessage>,
        tools: List<com.l2dchat.core.llm.LlmToolDefinition>,
        config: LlmGenerationConfig,
        toolExecutor: com.l2dchat.core.llm.LlmToolExecutor,
    ): LlmResponse =
        inner.chatCompletionWithTools(messages, tools, config, toolExecutor)

    /**
     * Seed the next run: full history + closing assistant text + feedback.
     *
     * @param feedback The validation feedback, delivered as a user message.
     * @param finalContent The previous run's closing assistant text (the loop
     *   returns it instead of appending it to the conversation).
     */
    fun continueWith(feedback: String, finalContent: String?) {
        val history = lastMessages.toMutableList()
        if (!finalContent.isNullOrBlank()) {
            history.add(LlmMessage.assistant(finalContent))
        }
        history.add(LlmMessage.user(feedback))
        seed = history
    }
}
