package com.l2dchat.core.mem

import java.time.ZoneId

/**
 * Library-side retrieval API — Kotlin port of `mem/retrieval.py::MemRetrieval`
 * (design section 5.1).
 *
 * The read face of the memory store: four APIs, no LLM — the only external
 * call is the query embedding. Every list answer goes through
 * [Operators.present] (progressive disclosure: summaries only, full detail
 * requires [expand]), and every API is a composition of the section-8 read
 * atoms plus the pure-function operators — nothing here opens its own storage
 * path.
 *
 * ## `Long` epoch seconds
 *
 * All time fields are epoch seconds as [Long] (never [Double]/[Float]), per
 * the port-wide convention. The Python reference uses `float`; [nowFn] returns
 * [Long] and the rank math internally promotes to [Double] (see [Operators.rank]).
 *
 * ## Mirrored Python semantics
 *
 * - [search] — `embed → vec top 3k → filter → rank → present`. `k <= 0`
 *   short-circuits to an empty list; an unresolvable `entity` yields an empty
 *   list; a bad `timeAxis` raises [ToolValidationError].
 * - [temporal] — interval-overlap scan via [MemStore.scanNodes]; uniform base
 *   score `1.0` so ordering is pure recency/boost plus entity weight.
 * - [entityEvents] — link join, `mentionTime` descending; no rank/present
 *   decay math, just a stable sort then [Operators.present].
 * - [expand] — full [Node] plus forward/reverse links, supersede chain, and
 *   predecessor list; stamps `accessCount + 1` via [MemStore.recordAccess].
 *
 * @param store The memory store; every read goes through its atoms.
 * @param embedder Query embedder — the only external call in this layer.
 * @param zoneId Timezone used by [TimeParse.parseEventTime] when expanding
 *   `time_range` strings. Defaults to [ZoneId.systemDefault] to mirror
 *   Python's `time.mktime`.
 * @param nowFn Clock for the rank decay/boost math, injected for tests.
 *   Returns epoch seconds as [Long].
 */
class MemRetrieval(
    private val store: MemStore,
    private val embedder: EmbeddingClient,
    private val zoneId: ZoneId = ZoneId.systemDefault(),
    private val nowFn: () -> Long = { System.currentTimeMillis() / 1000L },
) {
    /**
     * Semantic search: embed → vector recall → filter → rank → present.
     *
     * Pipeline (design sections 5.1/8.4): embed the query, take the top
     * [RECALL_FACTOR] * [k] vector candidates (R4), fetch each node (R1,
     * honoring [includeSuperseded]), then filter, rank (section 5.3) and
     * truncate to [k]. Filters, all optional:
     *
     * - [nodeType] — exact type match.
     * - [timeRange] — a normalized event_time expression parsed with
     *   [TimeParse.parseEventTime]; [timeAxis] `"event"` overlaps
     *   `[startTs, endTs)`, [timeAxis] `"mention"` applies the overlap
     *   operator to the mention-time point.
     * - [entity] — name/alias or hash; resolved like [entityEvents], then
     *   joined via the links table (R5).
     *
     * @param query Free-text query (embedded as-is).
     * @param k Max results after filtering and ranking. `k <= 0` returns an
     *   empty list (matching Python).
     * @param nodeType Optional exact node_type filter.
     * @param timeRange Optional normalized time expression (section 5.2).
     * @param timeAxis `"event"` or `"mention"` — which axis [timeRange]
     *   filters on. Defaults to `"event"`.
     * @param entity Optional entity name/alias or hash; restricts results to
     *   nodes linked to it.
     * @param includeSuperseded When `true`, superseded nodes stay eligible.
     * @return Up to [k] progressive-disclosure summaries, best first. An
     *   unresolvable [entity] yields an empty list.
     * @throws ToolValidationError On a bad [timeAxis] or an unparseable
     *   [timeRange].
     */
    suspend fun search(
        query: String,
        k: Int = 8,
        nodeType: String? = null,
        timeRange: String? = null,
        timeAxis: String = "event",
        entity: String? = null,
        includeSuperseded: Boolean = false,
    ): List<NodeSummary> {
        if (timeAxis !in TIME_AXES) {
            throw ToolValidationError(
                "search: time_axis must be one of ${TIME_AXES.toList()}, got '$timeAxis'"
            )
        }
        if (k <= 0) return emptyList()

        var queryInterval: Pair<Long, Long>? = null
        if (timeRange != null) {
            val (qStart, qEnd, _) = TimeParse.parseEventTime(timeRange, zoneId)
            queryInterval = qStart to qEnd
        }

        var eventHashes: Set<String>? = null
        if (entity != null) {
            val card = resolveEntity(entity) ?: return emptyList()
            eventHashes = store.getLinks(entityHash = card.hashId)
                .map { it.eventHash }
                .toSet()
        }

        val queryVec = embedder.embed(query)
        val candidates = store.vecTopK(queryVec, k = RECALL_FACTOR * k)
        val kept: MutableList<Pair<Node, Double>> = ArrayList(candidates.size)
        for ((hashId, score) in candidates) {
            val node = store.getNode(hashId, includeSuperseded = includeSuperseded)
                ?: continue
            if (nodeType != null && node.nodeType != nodeType) continue
            if (queryInterval != null && !inRange(node, queryInterval, timeAxis)) continue
            if (eventHashes != null && node.hashId !in eventHashes) continue
            kept.add(node to score)
        }
        return rankAndPresent(kept, k)
    }

    /**
     * Time-axis query: parse range → conditional scan → rank → present.
     *
     * [axis] `"mention"` keeps nodes whose mention time falls in the
     * half-open query interval (R3 `mention_range`); [axis] `"event"` keeps
     * nodes whose `[startTs, endTs)` overlaps it (R3 `time_overlap` —
     * touching endpoints do not overlap). All candidates share a uniform
     * base score of `1.0`, so ordering is pure recency/boost plus entity
     * weight.
     *
     * @param timeRange Normalized time expression (section 5.2).
     * @param axis `"mention"` or `"event"`. Defaults to `"mention"`.
     * @param k Max results. `k <= 0` returns an empty list.
     * @param nodeType Optional exact node_type filter.
     * @return Up to [k] progressive-disclosure summaries, best first.
     * @throws ToolValidationError On a bad [axis] or an unparseable
     *   [timeRange].
     */
    suspend fun temporal(
        timeRange: String,
        axis: String = "mention",
        k: Int = 20,
        nodeType: String? = null,
    ): List<NodeSummary> {
        if (axis !in TIME_AXES) {
            throw ToolValidationError(
                "temporal: axis must be one of ${TIME_AXES.toList()}, got '$axis'"
            )
        }
        val (qStart, qEnd, _) = TimeParse.parseEventTime(timeRange, zoneId)
        val nodes: List<Node> = if (axis == "mention") {
            store.scanNodes(
                nodeType = nodeType,
                mentionRange = qStart to qEnd,
                limit = SCAN_LIMIT,
            )
        } else {
            store.scanNodes(
                nodeType = nodeType,
                timeOverlap = qStart to qEnd,
                limit = SCAN_LIMIT,
            )
        }
        if (k <= 0) return emptyList()
        return rankAndPresent(nodes.map { it to 1.0 }, k)
    }

    /**
     * Everything about an entity: link join, mention-time descending.
     *
     * The reference is a name/alias (R2 exact match; on multiple hits the
     * highest `appearance_count` wins) or a hash (R1, which must validate as
     * `baseType == "entity"`). Events are fetched via the links table
     * (R5 → R1 batch); superseded events stay filtered (R1 default).
     *
     * @param entity Entity name/alias or hash.
     * @param k Max results. `k <= 0` returns an empty list.
     * @return Up to [k] progressive-disclosure summaries, most recently
     *   mentioned first; an empty list when the entity does not resolve.
     */
    suspend fun entityEvents(entity: String, k: Int = 20): List<NodeSummary> {
        if (k <= 0) return emptyList()
        val card = resolveEntity(entity) ?: return emptyList()
        val nodes: MutableList<Node> = ArrayList()
        for (link in store.getLinks(entityHash = card.hashId)) {
            val node = store.getNode(link.eventHash) ?: continue
            nodes.add(node)
        }
        nodes.sortWith(compareByDescending<Node> { it.mentionTime }.thenBy { it.hashId })
        val top = nodes.take(k)
        val (namesMap, _) = entityContext(top)
        return Operators.present(top, namesMap, zoneId)
    }

    /**
     * Full detail view of one node — the second hop after a summary.
     *
     * Returns the complete [Node] (full content, full metadata, access
     * counters *after* stamping this access) plus:
     *
     * - [ExpandedNode.entities] — forward links (R5) with `hash` + resolved
     *   `canonicalName`, `role` and `mentionCount`;
     * - [ExpandedNode.events] — reverse links (only nonempty for entity
     *   nodes) with `hash`, `role`, `mentionCount`, `mentionTime` and a
     *   truncated `preview`;
     * - [ExpandedNode.supersedeChain] — successor hashes, nearest first,
     *   followed via R1 until the chain ends (cycle-safe);
     * - [ExpandedNode.predecessors] — hashes of nodes superseded by this one,
     *   nearest first (transitive).
     *
     * Superseded nodes remain expandable. The visit is recorded as
     * `accessCount + 1` and a fresh `lastAccessed` ([MemStore.recordAccess]).
     *
     * **Note (mirrors Python)**: [hashId] is a full hash, not a prefix. The
     * Python reference's `expand(hash_id)` takes the full id directly; prefix
     * expansion is the caller's responsibility (the section-6 agent surface
     * resolves the 8-char summary prefix via [MemStore.resolveHash] before
     * calling [expand]).
     *
     * @param hashId The node to expand (full hash).
     * @return The detail record, or `null` when no such node exists (no
     *   access is recorded then).
     */
    suspend fun expand(hashId: String): ExpandedNode? {
        var node = store.getNode(hashId, includeSuperseded = true) ?: return null
        store.recordAccess(hashId)
        store.getNode(hashId, includeSuperseded = true)?.let { node = it }

        val entities: MutableList<EntityLinkView> = ArrayList()
        for (link in store.getLinks(eventHash = hashId)) {
            val card = store.getNode(link.entityHash, includeSuperseded = true)
            entities.add(
                EntityLinkView(
                    hash = link.entityHash,
                    canonicalName = card?.let { displayName(it) },
                    role = link.role,
                    mentionCount = link.mentionCount,
                )
            )
        }
        entities.sortBy { it.hash }

        val events: MutableList<EventLinkView> = ArrayList()
        for (link in store.getLinks(entityHash = hashId)) {
            val event = store.getNode(link.eventHash, includeSuperseded = true)
            events.add(
                EventLinkView(
                    hash = link.eventHash,
                    role = link.role,
                    mentionCount = link.mentionCount,
                    mentionTime = event?.mentionTime,
                    preview = event?.let { preview(it.content) },
                )
            )
        }
        events.sortWith(
            compareByDescending<EventLinkView> { it.mentionTime ?: 0L }.thenBy { it.hash }
        )

        return ExpandedNode(
            node = node,
            entities = entities,
            events = events,
            supersedeChain = successors(node),
            predecessors = predecessors(hashId),
        )
    }

    /* ------------------------------------------------------------------ */
    /* Internal helpers                                                   */
    /* ------------------------------------------------------------------ */

    /**
     * Apply the overlap operator to the node on the requested time axis.
     *
     * On the event axis the store always expands `startTs`/`endTs` on write;
     * the mention fallback only guards rows written around MemStore. On the
     * mention axis the node's `mentionTime` is treated as a zero-length
     * point interval.
     */
    private fun inRange(node: Node, queryInterval: Pair<Long, Long>, axis: String): Boolean {
        val (qStart, qEnd) = queryInterval
        return if (axis == "event") {
            val start = node.startTs ?: node.mentionTime
            val end = node.endTs ?: node.mentionTime
            Operators.overlap(qStart, qEnd, start, end)
        } else {
            Operators.overlap(qStart, qEnd, node.mentionTime, node.mentionTime)
        }
    }

    /**
     * Resolve an entity reference — a node hash or a name/alias.
     *
     * Hash path: the node must exist and be `baseType == "entity"`. Name
     * path: exact canonical/alias matches (R2); on multiple hits the one
     * with the highest `appearance_count` wins (ties keep scan order).
     */
    private suspend fun resolveEntity(ref: String): Node? {
        val node = store.getNode(ref)
        if (node != null) {
            return node.takeIf { it.baseType == "entity" }
        }
        val candidates = store.findEntities(ref)
        if (candidates.isEmpty()) return null
        return candidates.maxByOrNull { node ->
            ((node.metadata["appearance_count"] as? Number)?.toLong() ?: 0L)
        }
    }

    /**
     * Per-node linked-entity display names and appearance-count sums.
     *
     * Only live entities count (R1 default): the weight of a node is the sum
     * of `appearance_count` over the entities currently linked to it.
     */
    private suspend fun entityContext(nodes: List<Node>): Pair<Map<String, List<String>>, Map<String, Long>> {
        val namesMap: MutableMap<String, MutableList<String>> = LinkedHashMap()
        val weights: MutableMap<String, Long> = LinkedHashMap()
        for (node in nodes) {
            val names: MutableList<String> = ArrayList()
            var weight = 0L
            for (link in store.getLinks(eventHash = node.hashId)) {
                val entity = store.getNode(link.entityHash) ?: continue
                names.add(displayName(entity))
                weight += ((entity.metadata["appearance_count"] as? Number)?.toLong() ?: 0L)
            }
            names.sort()
            namesMap[node.hashId] = names
            weights[node.hashId] = weight
        }
        return namesMap to weights
    }

    /** Rank scored candidates (section 5.3), truncate to [k], then present. */
    private suspend fun rankAndPresent(
        scoredNodes: List<Pair<Node, Double>>,
        k: Int,
    ): List<NodeSummary> {
        val nodes = scoredNodes.map { it.first }
        val (namesMap, weights) = entityContext(nodes)
        val items = scoredNodes.map { (node, score) ->
            RankItem(
                id = node.hashId,
                score = score,
                mentionTime = node.mentionTime,
                startTs = node.startTs,
                entityWeight = weights[node.hashId] ?: 0L,
            )
        }
        val ordered = Operators.rank(items, now = nowFn()).take(k)
        // Re-derive the Node list in ranked order by hash lookup; the input
        // list is small (≤ 3k candidates) so a linear per-item scan is fine
        // and avoids a second store round-trip per item.
        val byHash: Map<String, Node> = nodes.associateBy { it.hashId }
        val rankedNodes = ordered.mapNotNull { byHash[it.id] }
        return Operators.present(rankedNodes, namesMap, zoneId)
    }

    /** Follow the superseded_by chain forward, nearest successor first. */
    private suspend fun successors(node: Node): List<String> {
        val chain: MutableList<String> = ArrayList()
        val seen: MutableSet<String> = HashSet()
        seen.add(node.hashId)
        var cursor = node
        while (true) {
            val nextHash = cursor.supersededBy ?: break
            if (nextHash in seen) break
            val successor = store.getNode(nextHash, includeSuperseded = true) ?: break
            seen.add(successor.hashId)
            chain.add(successor.hashId)
            cursor = successor
        }
        return chain
    }

    /**
     * Find (transitive) predecessors: nodes whose `superseded_by` leads here.
     *
     * Mirrors `_predecessors` in `retrieval.py`: scan every node (including
     * superseded ones) via [MemStore.scanNodes] with `includeSuperseded = true`,
     * build a `by_successor` map keyed on each node's `superseded_by`, then
     * BFS from [hashId] — at each frontier node, the predecessors are the
     * nodes pointing at it, sorted by `hashId` for deterministic order; each
     * new predecessor is appended to the chain and becomes the next frontier.
     * Cycle-safe via a `visited` set.
     */
    private suspend fun predecessors(hashId: String): List<String> {
        val bySuccessor: MutableMap<String, MutableList<Node>> = LinkedHashMap()
        for (other in store.scanNodes(limit = SCAN_LIMIT, includeSuperseded = true)) {
            val successor = other.supersededBy ?: continue
            bySuccessor.getOrPut(successor) { ArrayList() }.add(other)
        }
        val chain: MutableList<String> = ArrayList()
        val visited: MutableSet<String> = HashSet()
        visited.add(hashId)
        val frontier: ArrayDeque<String> = ArrayDeque()
        frontier.addLast(hashId)
        while (frontier.isNotEmpty()) {
            val current = frontier.removeFirst()
            val preds = bySuccessor[current] ?: continue
            for (pred in preds.sortedBy { it.hashId }) {
                if (pred.hashId in visited) continue
                visited.add(pred.hashId)
                chain.add(pred.hashId)
                frontier.addLast(pred.hashId)
            }
        }
        return chain
    }

    /** Render an entity's display name: canonical_name, else its content. */
    private fun displayName(entity: Node): String =
        (entity.metadata["canonical_name"] as? String) ?: entity.content

    /** Truncate to the progressive-disclosure preview length with a marker. */
    private fun preview(text: String): String =
        if (text.length > Operators.PREVIEW_LENGTH) {
            text.take(Operators.PREVIEW_LENGTH) + "[+]"
        } else {
            text
        }

    /* ------------------------------------------------------------------ */
    /* Public result types                                                */
    /* ------------------------------------------------------------------ */

    /**
     * One forward (event → entity) link view attached to an [ExpandedNode].
     *
     * @property hash The entity node's full hash id.
     * @property canonicalName The entity's display name (canonical_name, else
     *   content), or `null` when the entity card no longer exists.
     * @property role The link role label.
     * @property mentionCount How many times this edge was re-mentioned.
     */
    data class EntityLinkView(
        val hash: String,
        val canonicalName: String?,
        val role: String,
        val mentionCount: Int,
    )

    /**
     * One reverse (entity → event) link view attached to an [ExpandedNode].
     *
     * @property hash The event node's full hash id.
     * @property role The link role label.
     * @property mentionCount How many times this edge was re-mentioned.
     * @property mentionTime The event's mention epoch seconds, or `null` when
     *   the event node no longer exists.
     * @property preview Truncated content preview, or `null` when the event
     *   node no longer exists.
     */
    data class EventLinkView(
        val hash: String,
        val role: String,
        val mentionCount: Int,
        val mentionTime: Long?,
        val preview: String?,
    )

    /**
     * Full detail record returned by [expand].
     *
     * @property node The complete [Node] (after the access stamp).
     * @property entities Forward links (event → entity).
     * @property events Reverse links (entity → event); nonempty only for
     *   entity nodes.
     * @property supersedeChain Successor hashes, nearest first.
     * @property predecessors Predecessor hashes (transitive — see the private
     *   [predecessors] helper).
     */
    data class ExpandedNode(
        val node: Node,
        val entities: List<EntityLinkView>,
        val events: List<EventLinkView>,
        val supersedeChain: List<String>,
        val predecessors: List<String>,
    )

    companion object {
        /**
         * Vector recall width: search fetches [RECALL_FACTOR] * `k`
         * candidates before filtering and ranking (design section 5.1,
         * "top 3k"). Matches `RECALL_FACTOR` in `retrieval.py`.
         */
        const val RECALL_FACTOR: Int = 3

        /**
         * Scan ceiling for the recall step of temporal and expand's
         * predecessor scan. Matches `_SCAN_LIMIT` in `retrieval.py`.
         */
        const val SCAN_LIMIT: Int = 10_000

        /** Allowed time-axis values for search and temporal. */
        val TIME_AXES: Set<String> = setOf("event", "mention")
    }
}
