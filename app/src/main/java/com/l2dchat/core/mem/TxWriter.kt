package com.l2dchat.core.mem

/**
 * Transactional write stager for the mem library — Kotlin port of `mem/tx.py`.
 *
 * Agents never touch storage directly: every write atom (W1–W5) is staged on a
 * [TxWriter], and once the extraction loop's closing validation passes,
 * [flush] hands the ordered op list to [MemStore.apply] which runs it in a
 * single transaction (every op lands or none does).
 *
 * ## Staging semantics (mirrors `tx.py`)
 *
 * - [putNode] — content-addressed; the same `(nodeType, content, eventTime)`
 *   re-registered returns the existing staged hash without overwriting it.
 * - [updateNode] — appended in call order; field names must be in
 *   [UPDATABLE_FIELDS].
 * - [putLink] — deduplicated on `(eventHash, entityHash, role)`; a repeat link
 *   bumps the staged [TxOp.PutLink.mentionCount] instead of creating a second
 *   entry.
 * - [putVector] — deduplicated on [hashId]; the last vector wins. Dimension is
 *   hard-checked against [dimensions] up front.
 * - [putReceipt] — deduplicated on `batch_id`; the last receipt wins.
 *
 * ## Flush order
 *
 * `put_node` (insertion order) → `update_node` (call order) → `put_link`
 * (insertion order) → `put_vector` → `put_receipt`. Staging is cleared only
 * after [MemStore.apply] returns successfully; if it throws, everything stays
 * staged so the flush can be retried.
 *
 * @property dimensions Expected vector dimension for [putVector] hard checks.
 */
class TxWriter(private val dimensions: Int) {

    init {
        require(dimensions > 0) { "TxWriter: dimensions must be positive, got $dimensions" }
    }

    // LinkedHashMap so flush emits put_node ops in insertion order.
    private val nodes: MutableMap<String, TxOp.PutNode> = LinkedHashMap()
    private val updates: MutableList<TxOp.UpdateNode> = ArrayList()
    // Triple key (eventHash, entityHash, role) → staged link.
    private val links: MutableMap<LinkKey, TxOp.PutLink> = LinkedHashMap()
    private val vectors: MutableMap<String, TxOp.PutVector> = LinkedHashMap()
    private val receipts: MutableMap<String, TxOp.PutReceipt> = LinkedHashMap()

    /** Number of ops currently staged. */
    fun size(): Int =
        nodes.size + updates.size + links.size + vectors.size + receipts.size

    /**
     * Stage a node (write atom W1).
     *
     * The hash is content-addressed: `Hashing.shortHash(nodeType + content +
     * (eventTime ?: ""))`, mirroring the Python `short_hash(str(node_dict[
     * "node_type"]) + str(node_dict["content"]) + str(node_dict.get(
     * "event_time") or ""))`. A hash already staged means the same content was
     * registered twice: the existing staged row is returned unchanged (never
     * overwritten).
     *
     * @param nodeType Declarative node type (e.g. `"event"`, `"entity"`).
     * @param content The only text that gets embedded.
     * @param eventTime Normalized event_time string, or `null` when absent.
     * @param baseType `"content"` or `"entity"`.
     * @param mentionTime "Appearing when" epoch seconds.
     * @param eventTimeRaw Original (pre-normalization) event-time expression.
     * @param importance Optional model-assigned importance score.
     * @param sentiment Optional model-assigned sentiment score.
     * @param metadata Declarative per-type extension fields.
     * @return The node's 12-char hash id.
     */
    fun putNode(
        nodeType: String,
        content: String,
        eventTime: String? = null,
        baseType: String = "content",
        mentionTime: Long = System.currentTimeMillis() / 1000L,
        eventTimeRaw: String? = null,
        importance: Double? = null,
        sentiment: Double? = null,
        metadata: Map<String, Any?> = emptyMap(),
    ): String {
        val hashInput = nodeType + content + (eventTime ?: "")
        val hashId = Hashing.shortHash(hashInput)
        if (hashId !in nodes) {
            nodes[hashId] = TxOp.PutNode(
                hashId = hashId,
                baseType = baseType,
                nodeType = nodeType,
                content = content,
                mentionTime = mentionTime,
                eventTimeRaw = eventTimeRaw,
                eventTime = eventTime,
                importance = importance,
                sentiment = sentiment,
                metadata = metadata,
            )
        }
        return hashId
    }

    /**
     * Stage a field update on a node (write atom W2).
     *
     * Every key in [fields] must be in [UPDATABLE_FIELDS]; any other key throws
     * [ToolValidationError], matching the Python whitelist guard.
     *
     * @param hashId Target node hash (may refer to a stored or staged node).
     * @param fields Whitelisted fields to update.
     */
    fun updateNode(hashId: String, fields: Map<String, Any?>) {
        val unknown = fields.keys - UPDATABLE_FIELDS
        if (unknown.isNotEmpty()) {
            throw ToolValidationError(
                "update_node: fields not updatable: ${unknown.sorted()} " +
                    "(allowed: ${UPDATABLE_FIELDS.sorted()})"
            )
        }
        updates.add(TxOp.UpdateNode(hashId = hashId, fields = fields.toMap()))
    }

    /**
     * Stage an event-entity link (write atom W3).
     *
     * Deduplicates on the `(eventHash, entityHash, role)` primary key: a repeat
     * link does not create a second entry but bumps the staged link's
     * [TxOp.PutLink.mentionCount].
     *
     * @return `true` if the link was newly staged, `false` if it already existed.
     */
    fun putLink(eventHash: String, entityHash: String, role: String): Boolean {
        val key = LinkKey(eventHash, entityHash, role)
        val existing = links[key]
        if (existing != null) {
            links[key] = existing.copy(mentionCount = existing.mentionCount + 1)
            return false
        }
        links[key] = TxOp.PutLink(
            eventHash = eventHash,
            entityHash = entityHash,
            role = role,
            mentionCount = 1,
        )
        return true
    }

    /**
     * Stage a node embedding (write atom W4).
     *
     * @throws DimMismatchError If the vector dimension differs from
     *   [dimensions][TxWriter.dimensions].
     */
    fun putVector(hashId: String, vector: List<Float>) {
        if (vector.size != dimensions) {
            throw DimMismatchError(
                "put_vector: dim mismatch: expected $dimensions, got ${vector.size}"
            )
        }
        // Defensive copy so callers cannot mutate the staged data.
        vectors[hashId] = TxOp.PutVector(hashId = hashId, vector = vector.toList())
    }

    /**
     * Stage a batch receipt (write atom W5), deduplicated on `batchId`.
     *
     * The last receipt staged for a given `batchId` wins.
     */
    fun putReceipt(receipt: TxOp.PutReceipt) {
        receipts[receipt.batchId] = receipt
    }

    /**
     * Stage a batch receipt (write atom W5) from raw fields, deduplicated on
     * [batchId].
     */
    fun putReceipt(
        batchId: String,
        source: String?,
        status: String,
        nodeHashes: List<String> = emptyList(),
        linkCount: Int = 0,
        error: String? = null,
        createdAt: Long = System.currentTimeMillis() / 1000L,
    ): TxOp.PutReceipt {
        val op = TxOp.PutReceipt(
            batchId = batchId,
            source = source,
            status = status,
            nodeHashes = nodeHashes.toList(),
            linkCount = linkCount,
            error = error,
            createdAt = createdAt,
        )
        receipts[batchId] = op
        return op
    }

    /**
     * Hand all staged ops to [store] as one ordered transaction.
     *
     * Op order: `put_node` (insertion order) → `update_node` (call order) →
     * `put_link` (insertion order) → `put_vector` → `put_receipt`. Staging is
     * cleared only after [MemStore.apply] returns successfully; if it throws,
     * everything stays staged so the flush can be retried. Flushing an empty
     * staging area is a no-op (the store is not called).
     */
    suspend fun flush(store: MemStore) {
        val ops = stagedOps()
        if (ops.isEmpty()) return
        store.apply(ops)
        clear()
    }

    /** Build the ordered op list from the staging area (does not clear). */
    fun stagedOps(): List<TxOp> {
        val out: MutableList<TxOp> = ArrayList(
            nodes.size + updates.size + links.size + vectors.size + receipts.size
        )
        out.addAll(nodes.values)
        out.addAll(updates)
        out.addAll(links.values)
        out.addAll(vectors.values)
        out.addAll(receipts.values)
        return out
    }

    /** Drop everything currently staged. */
    fun clear() {
        nodes.clear()
        updates.clear()
        links.clear()
        vectors.clear()
        receipts.clear()
    }

    /** Composite key for link deduplication. */
    private data class LinkKey(val eventHash: String, val entityHash: String, val role: String)
}

/**
 * Fields that [TxWriter.updateNode] / [TxOp.UpdateNode] is allowed to touch
 * (design section 8.1, W2). Mirrors `tx.py::UPDATABLE_FIELDS`.
 */
val UPDATABLE_FIELDS: Set<String> = setOf(
    "metadata",
    "aliases",
    "superseded_by",
    "appearance_count",
    "access_count",
)

/**
 * Sealed hierarchy of staged write ops — the typed Kotlin equivalent of the
 * Python op dicts (`{"op": "put_node", "node": {...}}`, etc.).
 *
 * Each op carries exactly the fields [MemStore.apply] needs to execute it; the
 * op kind is encoded by the subtype so `when` exhaustiveness replaces string
 * dispatch.
 */
sealed class TxOp {

    /** W1 — insert a node; content-hash conflicts are ignored, never replaced. */
    data class PutNode(
        val hashId: String,
        val baseType: String,
        val nodeType: String,
        val content: String,
        val mentionTime: Long,
        val eventTimeRaw: String?,
        val eventTime: String?,
        val importance: Double?,
        val sentiment: Double?,
        val metadata: Map<String, Any?>,
    ) : TxOp()

    /** W2 — whitelisted read-modify-write on an existing node. */
    data class UpdateNode(
        val hashId: String,
        val fields: Map<String, Any?>,
    ) : TxOp()

    /** W3 — upsert a directed event→entity edge; [mentionCount] accumulates on PK conflict. */
    data class PutLink(
        val eventHash: String,
        val entityHash: String,
        val role: String,
        val mentionCount: Int,
    ) : TxOp()

    /** W4 — insert or replace a node embedding (dimension pre-validated by [TxWriter]). */
    data class PutVector(
        val hashId: String,
        val vector: List<Float>,
    ) : TxOp()

    /** W5 — upsert a batch receipt keyed on [batchId]. */
    data class PutReceipt(
        val batchId: String,
        val source: String?,
        val status: String,
        val nodeHashes: List<String>,
        val linkCount: Int,
        val error: String?,
        val createdAt: Long,
    ) : TxOp()
}
