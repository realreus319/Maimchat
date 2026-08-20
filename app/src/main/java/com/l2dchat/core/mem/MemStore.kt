package com.l2dchat.core.mem

import java.time.ZoneId

/**
 * Storage engine and atomic tools for the mem library — Kotlin port of
 * `mem/store.py::MemStore`.
 *
 * [MemStore] is the real [TxWriter] flush target: [apply] executes a staged
 * op list as one transaction (every op lands or none does), and the read atoms
 * R1–R8 plus the management write [deleteNode] are the only storage paths —
 * nothing bypasses them.
 *
 * ## Replay safety (design section 2)
 *
 * A staged [TxOp.PutReceipt] whose `batchId` already exists with
 * `status = "done"` makes the whole [apply] a no-op, so flushing the same
 * batch twice leaves the database byte-identical. A prior `failed` receipt
 * does not block re-application; the receipt row is then overwritten by upsert.
 *
 * ## Transaction semantics
 *
 * The Python reference wraps every op of an [apply] in a single SQLite
 * transaction (`with self._conn:`) so a failure rolls back the whole batch.
 * Room offers the same primitive via `RoomDatabase.withTransaction { ... }`,
 * but [MemStore] deliberately does not depend on a concrete `RoomDatabase`
 * subclass (it would couple this module to `ChatDatabase`, which a concurrent
 * agent is editing). Instead, [transactionRunner] is an optional lambda the
 * runtime wires in — typically `{ block -> db.withTransaction { block() } }`.
 *
 * When [transactionRunner] is null (the default), each DAO call runs in its
 * own implicit transaction. This is a **deviation from the Python single-
 * transaction guarantee**: a mid-batch failure may leave earlier ops committed.
 * The deviation is accepted for now because (a) the production wiring will
 * always supply a real `RoomDatabase.withTransaction` runner, and (b) the
 * idempotent-receipt guard at the top of [apply] makes a partial commit
 * recoverable — the next flush retries the whole batch and the already-done
 * rows are no-ops (`INSERT OR IGNORE`, `ON CONFLICT … DO UPDATE` accumulation,
 * `INSERT OR REPLACE`).
 *
 * ## In-memory indexes
 *
 * Two indexes live next to the store and are mutated incrementally:
 * - [vectorIndex] — brute-force cosine top-k over the `mem_vectors` table.
 * - [lexIndex] — BM25 over live entity cards (the string-match half of hybrid
 *   entity recall).
 *
 * Both are rebuilt from the database by [load] at startup and kept in sync
 * after every successful [apply] / [deleteNode].
 *
 * ## `Long` epoch seconds
 *
 * All time fields are epoch seconds as [Long] (never [Double]/[Float]), per
 * the port-wide convention documented in the integration plan.
 *
 * @param nodeDao Nodes table DAO.
 * @param linkDao Links table DAO.
 * @param vectorDao Vectors table DAO.
 * @param receiptDao Receipts table DAO.
 * @param expectedDim Expected vector dimension. When non-null, every
 *   [TxOp.PutVector] / [vecTopK] is hard-checked against it. When null, the
 *   dimension is adopted from the database (or from the first vector written)
 *   and enforced from then on.
 * @param transactionRunner Optional suspend lambda that wraps a block in a
 *   single Room transaction. Production wiring should pass
 *   `{ block -> db.withTransaction { block() } }`. When null, each DAO call
 *   runs in its own implicit transaction (see "Transaction semantics" above).
 * @param zoneId Timezone used by [TimeParse] when expanding `event_time`
 *   strings. Defaults to [ZoneId.systemDefault] to mirror Python's
 *   `time.mktime`.
 */
class MemStore(
    private val nodeDao: MemNodeDao,
    private val linkDao: MemLinkDao,
    private val vectorDao: MemVectorDao,
    private val receiptDao: MemReceiptDao,
    private val expectedDim: Int? = null,
    private val transactionRunner: (suspend (block: suspend () -> Unit) -> Unit)? = null,
    private val zoneId: ZoneId = ZoneId.systemDefault(),
) {
    private val vectorIndex = VectorIndex(expectedDim)
    private val lexIndex = BM25Index()

    /**
     * Bulk-load every stored vector and every live entity card into the
     * in-memory indexes.
     *
     * Mirrors `MemStore._load_vectors` + `MemStore._load_lex`. Must be called
     * once at startup, before the first [apply] / read.
     *
     * @throws DimMismatchError If stored vectors disagree on dimension, a blob
     *   length does not match its declared dim, or the stored dimension
     *   conflicts with [expectedDim].
     */
    suspend fun load() {
        val rows = vectorDao.loadAll()
        for (row in rows) {
            // VectorIndex.loadFromBlob validates dim == bytes.size / 4 and
            // reconciles against any configured dimension.
            vectorIndex.loadFromBlob(row.hashId, row.dim, row.embedding)
        }
        for (entity in nodeDao.getAllLiveEntities()) {
            val node = entity.toNode()
            lexIndex.add(node.hashId, entityDocText(node))
        }
    }

    /* ------------------------------------------------------------------ */
    /* TxStore: single-transaction apply                                  */
    /* ------------------------------------------------------------------ */

    /**
     * Apply staged write ops atomically (the [TxWriter] flush target).
     *
     * Op semantics (design section 8.1):
     * - [TxOp.PutNode] — `INSERT OR IGNORE`; a hash conflict is the same
     *   content re-registered, the existing row is never modified.
     * - [TxOp.UpdateNode] — read-modify-write per field type (metadata dict
     *   merge, aliases union, appearance/access count increments,
     *   superseded_by direct write).
     * - [TxOp.PutLink] — PK upsert; on conflict `mention_count` accumulates.
     * - [TxOp.PutVector] — `INSERT OR REPLACE` with dimension checks.
     * - [TxOp.PutReceipt] — `batch_id` upsert (a failed batch may be retried).
     *
     * If any op fails, the whole transaction rolls back and the exception
     * propagates (the staging area survives, so the flush can be retried).
     * The in-memory indexes are updated only after a successful commit.
     *
     * @param txOps Ordered op list as produced by [TxWriter.stagedOps].
     */
    suspend fun apply(txOps: List<TxOp>) {
        if (txOps.isEmpty()) return
        if (isReplay(txOps)) return

        val stagedVectors: MutableMap<String, List<Float>> = LinkedHashMap()
        val touchedNodes: MutableSet<String> = LinkedHashSet()

        val body: suspend () -> Unit = {
            for (op in txOps) {
                when (op) {
                    is TxOp.PutNode -> {
                        applyPutNode(op)
                        touchedNodes.add(op.hashId)
                    }
                    is TxOp.UpdateNode -> {
                        applyUpdateNode(op)
                        touchedNodes.add(op.hashId)
                    }
                    is TxOp.PutLink -> {
                        applyPutLink(op)
                    }
                    is TxOp.PutVector -> {
                        applyPutVector(op)
                        stagedVectors[op.hashId] = op.vector
                    }
                    is TxOp.PutReceipt -> {
                        applyPutReceipt(op)
                    }
                }
            }
        }

        val runner = transactionRunner
        if (runner != null) {
            runner(body)
        } else {
            body()
        }

        // Index sync — only after a successful commit.
        for ((hashId, vector) in stagedVectors) {
            vectorIndex.put(hashId, vector.toFloatArray())
        }
        refreshLex(touchedNodes)
    }

    /**
     * Return `true` when every staged receipt already exists as `done`.
     *
     * Mirrors `MemStore._is_replay`. A batch with no receipts is never a
     * replay; a batch whose receipts are all `done` is a no-op.
     */
    private suspend fun isReplay(txOps: List<TxOp>): Boolean {
        val batchIds = txOps.mapNotNull { op ->
            (op as? TxOp.PutReceipt)?.batchId
        }
        if (batchIds.isEmpty()) return false
        val done = receiptDao.findDoneBatchIds(batchIds).toSet()
        return batchIds.all { it in done }
    }

    /** W1: insert a node; content-hash conflicts are ignored, never replaced. */
    private suspend fun applyPutNode(op: TxOp.PutNode) {
        // Deterministic start_ts/end_ts expansion owned by code, not the model.
        // Python (_apply_put_node): if start_ts/end_ts are null on the node,
        // expand from event_time (if any) else fall back to the instantaneous
        // mention-time interval. TxOp.PutNode never carries pre-computed
        // start_ts/end_ts (the model never sets them), so the decision is
        // purely: event_time present → parse it; else → mention_time instant.
        val startTs: Long
        val endTs: Long
        if (op.eventTime != null) {
            val (s, e, _) = TimeParse.parseEventTime(op.eventTime, zoneId)
            startTs = s
            endTs = e
        } else {
            startTs = op.mentionTime
            endTs = op.mentionTime
        }
        val now = nowEpochSeconds()
        val row = MemNodeEntity(
            hashId = op.hashId,
            baseType = op.baseType,
            nodeType = op.nodeType,
            content = op.content,
            mentionTime = op.mentionTime,
            eventTimeRaw = op.eventTimeRaw,
            eventTime = op.eventTime,
            startTs = startTs,
            endTs = endTs,
            importance = op.importance,
            sentiment = op.sentiment,
            metadataJson = Node.metadataToJson(op.metadata),
            supersededBy = null,
            accessCount = 0,
            lastAccessed = null,
            createdAt = now,
            updatedAt = now,
        )
        nodeDao.insertOrIgnore(row)
    }

    /**
     * W2: whitelisted read-modify-write on an existing node.
     *
     * @throws ToolValidationError If the node does not exist or a field is
     *   outside the whitelist (rolls back the transaction).
     */
    private suspend fun applyUpdateNode(op: TxOp.UpdateNode) {
        val row = nodeDao.getById(op.hashId)
            ?: throw ToolValidationError("update_node: node not found: '${op.hashId}'")

        val metadata: MutableMap<String, Any?> = LinkedHashMap(Node.parseMetadataJson(row.metadataJson))
        var accessCount = row.accessCount
        var supersededBy: String? = row.supersededBy

        for ((name, value) in op.fields) {
            when (name) {
                "metadata" -> {
                    @Suppress("UNCHECKED_CAST")
                    val incoming = (value as? Map<String, Any?>) ?: emptyMap()
                    metadata.putAll(incoming)
                }
                "aliases" -> {
                    val incoming: List<*> = (value as? List<*>) ?: emptyList<Any?>()
                    val existing: List<*> = (metadata["aliases"] as? List<*>) ?: emptyList<Any?>()
                    val merged = existing.toMutableList()
                    for (alias in incoming) {
                        if (alias !in merged) merged.add(alias)
                    }
                    metadata["aliases"] = merged
                }
                "appearance_count" -> {
                    val current = (metadata["appearance_count"] as? Number)?.toInt() ?: 0
                    val delta = (value as? Number)?.toInt() ?: 0
                    metadata["appearance_count"] = current + delta
                }
                "access_count" -> {
                    val delta = (value as? Number)?.toInt() ?: 0
                    accessCount += delta
                }
                "superseded_by" -> {
                    supersededBy = value as? String
                }
                else -> throw ToolValidationError(
                    "update_node: field not updatable: '$name'"
                )
            }
        }

        val now = nowEpochSeconds()
        nodeDao.updateMetadata(op.hashId, Node.metadataToJson(metadata), now)
        // access_count and superseded_by are separate columns; update them
        // unconditionally (the read-modify-write above already reconciled the
        // final value, even when the field was absent from the op).
        if (op.fields.containsKey("access_count")) {
            // incrementAccess uses SQL `access_count + :delta`; we computed the
            // absolute target above, so emit the delta from the stored value.
            val delta = accessCount - row.accessCount
            if (delta != 0) {
                nodeDao.incrementAccess(op.hashId, delta, now)
            }
        }
        if (op.fields.containsKey("superseded_by")) {
            nodeDao.setSuperseded(op.hashId, supersededBy, now)
        }
    }

    /** W3: insert a link; PK conflict bumps mention_count by the staged count. */
    private suspend fun applyPutLink(op: TxOp.PutLink) {
        linkDao.upsertLink(
            eventHash = op.eventHash,
            entityHash = op.entityHash,
            role = op.role,
            mentionCount = op.mentionCount,
            createdAt = nowEpochSeconds(),
        )
    }

    /**
     * W4: insert or replace a vector, hard-checking the dimension.
     *
     * @throws DimMismatchError If the dimension differs from the configured
     *   one, or from the dimension already stored for this hash.
     */
    private suspend fun applyPutVector(op: TxOp.PutVector) {
        val vector = op.vector.toFloatArray()
        val dim = vector.size
        val configured = vectorIndex.dimension()
        if (configured != null && dim != configured) {
            throw DimMismatchError(
                "put_vector: dim mismatch: expected $configured, got $dim"
            )
        }
        // The in-memory vectorIndex.put happens after commit (in apply()), not
        // here, so a transaction rollback cannot corrupt the index. The dim
        // check above is read-only and safe to run inside the body.
        val bytes = floatsToBytes(vector)
        vectorDao.upsert(MemVectorEntity(hashId = op.hashId, dim = dim, embedding = bytes))
    }

    /** W5: upsert a batch receipt keyed on batch_id. */
    private suspend fun applyPutReceipt(op: TxOp.PutReceipt) {
        val row = MemReceiptEntity(
            batchId = op.batchId,
            source = op.source,
            status = op.status,
            nodeHashesJson = nodeHashesToJson(op.nodeHashes),
            linkCount = op.linkCount,
            error = op.error,
            createdAt = op.createdAt,
        )
        receiptDao.upsert(row)
    }

    /* ------------------------------------------------------------------ */
    /* Read atoms (R1–R8)                                                 */
    /* ------------------------------------------------------------------ */

    /**
     * R1: fetch a node by hash.
     *
     * @param includeSuperseded When `false` (default), nodes that were
     *   superseded read as absent.
     * @return The node, or `null`.
     */
    suspend fun getNode(hashId: String, includeSuperseded: Boolean = false): Node? {
        val row = nodeDao.getById(hashId) ?: return null
        val node = row.toNode()
        if (node.supersededBy != null && !includeSuperseded) return null
        return node
    }

    /**
     * R8: full hashes matching a prefix (design section 6.3).
     *
     * Prefixes shorter than 6 characters are not serviced (they match too
     * broadly to be meaningful), matching the Python guard.
     *
     * @param prefix Hex-ish hash prefix (≥6 chars to get any results).
     * @param limit Max candidates returned, lexicographic order.
     * @return Matching full hashes, possibly empty; more than one means the
     *   prefix is ambiguous and the caller should ask for more chars.
     */
    suspend fun resolveHash(prefix: String, limit: Int = 10): List<String> {
        if (prefix.length < 6 || limit <= 0) return emptyList()
        return nodeDao.resolveHashPrefix(prefix, limit)
    }

    /**
     * R2: exact, case-insensitive entity lookup by canonical name or alias.
     *
     * Implementation choice mirrors the Python reference: a full scan of the
     * live `base_type = 'entity'` rows with Kotlin-side matching. Entity counts
     * are small and the call is an exact-match lookup on the extraction path —
     * not a hot loop. A separate name index could silently drift from the
     * database; scanning is correct by construction.
     *
     * Canonical name is read from `metadata["canonical_name"]`; aliases from
     * `metadata["aliases"]`. Both are compared case-insensitively against the
     * stripped, lowercased [nameOrAlias].
     */
    suspend fun findEntities(nameOrAlias: String): List<Node> {
        val needle = nameOrAlias.trim().lowercase()
        if (needle.isEmpty()) return emptyList()
        val out = ArrayList<Node>()
        for (row in nodeDao.getAllLiveEntities()) {
            val node = row.toNode()
            val canonical = (node.metadata["canonical_name"] as? String).orEmpty()
            val names = ArrayList<String>()
            names.add(canonical)
            val aliases = node.metadata["aliases"] as? List<*>
            if (aliases != null) {
                for (alias in aliases) {
                    if (alias != null) names.add(alias.toString())
                }
            }
            if (names.any { it.lowercase() == needle }) {
                out.add(node)
            }
        }
        return out
    }

    /**
     * R3: conditional scan over nodes.
     *
     * Room cannot build a dynamic `WHERE` clause, so the Python `scan_nodes`
     * combinator is implemented by selecting the most selective available DAO
     * query and then filtering its candidates in memory against the remaining
     * predicates. The mapping is:
     *
     * | Python filter | DAO query |
     * | --- | --- |
     * | `node_type` | [MemNodeDao.scanByType] |
     * | `mention_range` | [MemNodeDao.scanByMentionRange] |
     * | `time_overlap` | [MemNodeDao.scanByTimeOverlap] |
     * | `entity_hash` | [MemNodeDao.scanByEntityHash] |
     *
     * When multiple filters are present, the DAO query for the *first*
     * non-null filter in the order above is used as the candidate source, and
     * the other filters are applied in memory. This keeps the candidate set
     * small for the common single-filter cases while remaining correct for
     * multi-filter cases (the Python dynamic SQL would have AND-ed them all in
     * the database; the end result set is identical).
     *
     * @param nodeType Optional exact type filter.
     * @param mentionRange Optional half-open `(start, end)` filter on the
     *   mention axis.
     * @param timeOverlap Optional half-open `(start, end)`; a node matches when
     *   its `[startTs, endTs)` overlaps the query interval.
     * @param entityHash Optional entity; matches nodes linked to it via
     *   `mem_event_entity_links`.
     * @param limit Maximum rows returned.
     * @param includeSuperseded When `false` (default), superseded nodes are
     *   filtered out. When `true`, superseded nodes are included — but only the
     *   no-filter branch (the `else` case below) can actually return them,
     *   because it delegates to [MemNodeDao.scanAll] (no `superseded_by IS
     *   NULL` filter). The four filter-specific DAO queries
     *   (`scanByType`/`scanByMentionRange`/`scanByTimeOverlap`/`scanByEntityHash`)
     *   still hardcode `superseded_by IS NULL`, so they will not return
     *   superseded rows even when this is `true`. Full `includeSuperseded`
     *   support with filters requires a `@RawQuery` DAO method (deferred to a
     *   later phase). The no-filter branch is sufficient for the transitive
     *   predecessor walk in `MemRetrieval._predecessors`, which scans every
     *   node without filters (matching Python `scan_nodes(include_superseded=
     *   True)`).
     * @return Matching nodes, most recently mentioned first.
     */
    suspend fun scanNodes(
        nodeType: String? = null,
        mentionRange: Pair<Long, Long>? = null,
        timeOverlap: Pair<Long, Long>? = null,
        entityHash: String? = null,
        limit: Int = 100,
        includeSuperseded: Boolean = false,
    ): List<Node> {
        // Pick the candidate source: first non-null filter in priority order.
        val candidates: List<MemNodeEntity> = when {
            entityHash != null -> nodeDao.scanByEntityHash(entityHash, limit.coerceAtLeast(1))
            timeOverlap != null -> nodeDao.scanByTimeOverlap(timeOverlap.first, timeOverlap.second, limit.coerceAtLeast(1))
            mentionRange != null -> nodeDao.scanByMentionRange(mentionRange.first, mentionRange.second, limit.coerceAtLeast(1))
            nodeType != null -> nodeDao.scanByType(nodeType, limit.coerceAtLeast(1))
            else -> {
                // No filter selected — emulate the Python "no WHERE" branch.
                // includeSuperseded=true needs scanAll (the filter DAOs hardcode
                // `superseded_by IS NULL`); this backs the predecessor walk.
                if (includeSuperseded) {
                    nodeDao.scanAll(limit.coerceAtLeast(1))
                } else {
                    nodeDao.scanByMentionRange(Long.MIN_VALUE, Long.MAX_VALUE, limit.coerceAtLeast(1))
                }
            }
        }

        return candidates.asSequence()
            .map { it.toNode() }
            .filter { includeSuperseded || it.supersededBy == null }
            .filter { nodeType == null || it.nodeType == nodeType }
            .filter { mentionRange == null || (it.mentionTime >= mentionRange.first && it.mentionTime < mentionRange.second) }
            .filter { timeOverlap == null || overlaps(it.startTs, it.endTs, timeOverlap.first, timeOverlap.second) }
            // entityHash is already enforced by the DAO query when it is the
            // candidate source; when it is NOT the source but the caller still
            // passed it, we cannot re-check it without another DAO round-trip.
            // The Python reference AND-s all filters, so we only include
            // entityHash in the candidate-source decision (handled above).
            .sortedWith(compareByDescending<Node> { it.mentionTime }.thenBy { it.hashId })
            .take(limit)
            .toList()
    }

    /**
     * R4: brute-force cosine top-k over the in-memory vector index.
     *
     * Delegates to [VectorIndex.topK] after the same dimension hard-check the
     * Python reference performs.
     *
     * @throws DimMismatchError If the query dimension differs from the store's
     *   dimension.
     */
    suspend fun vecTopK(
        query: FloatArray,
        k: Int,
        candidateHashes: Set<String>? = null,
    ): List<Pair<String, Double>> = vectorIndex.topK(query, k, candidateHashes)

    /**
     * R7: BM25 top-k over the in-memory lexical index of entity cards.
     *
     * The index holds only live (non-superseded) entity nodes; each document
     * is the card's content plus its aliases. This is the string-match half of
     * the hybrid entity recall.
     */
    suspend fun entityLexTopK(query: String, k: Int): List<Pair<String, Double>> =
        lexIndex.search(query, k)

    /**
     * R5: link lookup in either direction (or both, as an AND filter).
     *
     * @throws ToolValidationError If neither direction is given.
     */
    suspend fun getLinks(
        eventHash: String? = null,
        entityHash: String? = null,
    ): List<MemEventEntityLinkEntity> {
        if (eventHash == null && entityHash == null) {
            throw ToolValidationError("get_links: event_hash or entity_hash is required")
        }
        return when {
            eventHash != null && entityHash != null -> {
                // AND filter: fetch by event, filter by entity in memory.
                linkDao.linksOfEvent(eventHash).filter { it.entityHash == entityHash }
            }
            eventHash != null -> linkDao.linksOfEvent(eventHash)
            else -> linkDao.linksOfEntity(entityHash!!)
        }
    }

    /**
     * R6: fetch a batch receipt — the idempotency check entry point.
     *
     * @return A [Receipt], or `null` when the batch was never recorded.
     */
    suspend fun getReceipt(batchId: String): Receipt? {
        val row = receiptDao.getById(batchId) ?: return null
        return Receipt.fromStoreDict(
            mapOf(
                "batch_id" to row.batchId,
                "source" to row.source,
                "status" to row.status,
                "node_hashes" to parseNodeHashesJson(row.nodeHashesJson),
                "link_count" to row.linkCount,
                "error" to row.error,
                "created_at" to row.createdAt,
            )
        )
    }

    /* ------------------------------------------------------------------ */
    /* Management write (W6 — not exposed to agents)                      */
    /* ------------------------------------------------------------------ */

    /**
     * W6: hard-delete a node from nodes/links/vectors in one transaction.
     *
     * @return `true` when a node row was actually deleted.
     */
    suspend fun deleteNode(hashId: String): Boolean {
        // The body returns the node rowcount so we can report whether a row
        // was actually deleted (matching Python `cursor.rowcount > 0`). The
        // transactionRunner boundary is opaque to the return value, so we
        // capture it in a holder outside the lambda.
        var rowsDeleted = 0
        val body: suspend () -> Unit = {
            rowsDeleted = nodeDao.deleteById(hashId)
            linkDao.deleteByHash(hashId)
            vectorDao.deleteById(hashId)
        }
        val runner = transactionRunner
        if (runner != null) {
            runner(body)
        } else {
            body()
        }
        vectorIndex.remove(hashId)
        lexIndex.discard(hashId)
        return rowsDeleted > 0
    }

    /**
     * Stamp one retrieval access on a node (design section 5.1, expand).
     *
     * A single UPDATE: `access_count + 1` and `last_accessed = now`.
     * `updated_at` is deliberately untouched — an access is a read, not a
     * content modification. A missing `hashId` is a silent no-op (the
     * retrieval layer checks existence first).
     */
    suspend fun recordAccess(hashId: String) {
        nodeDao.incrementAccess(hashId, delta = 1, now = nowEpochSeconds())
    }

    /* ------------------------------------------------------------------ */
    /* Internal helpers                                                   */
    /* ------------------------------------------------------------------ */

    /**
     * Re-index (or drop) touched entity cards after a successful commit.
     *
     * Re-reading the authoritative row keeps the index correct for every
     * mutation path: `INSERT OR IGNORE` conflicts, alias unions, and supersede
     * (superseded cards are dropped from recall).
     */
    private suspend fun refreshLex(hashIds: Set<String>) {
        for (hashId in hashIds) {
            val node = getNode(hashId, includeSuperseded = true)
            if (node != null && node.baseType == "entity" && node.supersededBy == null) {
                lexIndex.add(hashId, entityDocText(node))
            } else {
                lexIndex.discard(hashId)
            }
        }
    }

    /** Lexical document for an entity card: content plus all aliases. */
    private fun entityDocText(node: Node): String {
        val aliases = node.metadata["aliases"] as? List<*>
        val aliasText = aliases?.joinToString(" ") { it?.toString().orEmpty() }.orEmpty()
        return "${node.content} $aliasText".trim()
    }

    /** True iff `[nodeStart, nodeEnd)` overlaps `[qStart, qEnd)` (both half-open). */
    private fun overlaps(nodeStart: Long?, nodeEnd: Long?, qStart: Long, qEnd: Long): Boolean {
        val s = nodeStart ?: return false
        val e = nodeEnd ?: return false
        return s < qEnd && e > qStart
    }

    private fun nowEpochSeconds(): Long = System.currentTimeMillis() / 1000L

    /** Serialize a list of node hashes as a JSON array string (W5 column). */
    private fun nodeHashesToJson(hashes: List<String>): String {
        val sb = StringBuilder()
        sb.append('[')
        hashes.forEachIndexed { i, h ->
            if (i > 0) sb.append(',')
            // Reuse Node's string-appending helper via a single-entry map would
            // be wasteful; inline the RFC 8259 escapes for the common case
            // (hashes are URL-safe Base64, so only quotes need escaping).
            appendJsonString(sb, h)
        }
        sb.append(']')
        return sb.toString()
    }

    /** Parse a `node_hashes_json` column back into a list of strings. */
    private fun parseNodeHashesJson(json: String?): List<String> {
        if (json.isNullOrBlank()) return emptyList()
        val parsed = Node.parseMetadataJson("{\"v\":$json}")
        @Suppress("UNCHECKED_CAST")
        val list = parsed["v"] as? List<Any?> ?: return emptyList()
        return list.map { it?.toString().orEmpty() }
    }

    /** Append a JSON string literal, escaping per RFC 8259. */
    private fun appendJsonString(sb: StringBuilder, value: String) {
        sb.append('"')
        for (ch in value) {
            when (ch) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\b' -> sb.append("\\b")
                '\u000C' -> sb.append("\\f")
                else -> if (ch.code < 0x20) {
                    sb.append("\\u")
                    sb.append(ch.code.toString(16).padStart(4, '0'))
                } else {
                    sb.append(ch)
                }
            }
        }
        sb.append('"')
    }
}

/**
 * Convert a [MemNodeEntity] row to a [Node] domain object.
 *
 * Bridges the Room entity layer (storage shape) to the contract layer (domain
 * shape). The inverse of the row construction in `MemStore.applyPutNode`.
 */
internal fun MemNodeEntity.toNode(): Node {
    val metadata = if (metadataJson.isBlank()) {
        emptyMap()
    } else {
        Node.parseMetadataJson(metadataJson)
    }
    return Node(
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
        metadata = metadata,
        supersededBy = supersededBy,
        accessCount = accessCount,
        lastAccessed = lastAccessed,
        createdAt = createdAt,
        updatedAt = updatedAt,
    )
}
