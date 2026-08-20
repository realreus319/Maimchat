package com.l2dchat.core.mem

import com.l2dchat.core.llm.LlmToolDefinition
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * Agent-facing tools of the extraction flow — Kotlin port of
 * `mem/agent_tools.py` (design doc sections 2/3).
 *
 * [buildAgentTools] assembles the 8 tools that are the extraction agent's ONLY
 * read/write channel:
 *
 * - Read tools (`search_entities`, `get_entity`, `entity_events`) query the
 *   store directly through the read atoms.
 * - Write tools (`register_entity`, `register_content`, `link_event_entity`,
 *   `update_entity_aliases`, `supersede`) stage onto a [TxWriter] and never
 *   touch the store; the pipeline flushes everything in one transaction after
 *   its closing validation.
 *
 * Batch context (`mention_time`, `batch_metadata`) is injected by the pipeline
 * when binding the tools — the model never sets `mention_time`, and batch-level
 * metadata (e.g. life_capture modality/media_path) is merged into every staged
 * content node.
 *
 * ## In-batch visibility
 *
 * The tools keep a small closure-side view of what they staged (entities by
 * name/alias, content hashes, supersede marks), so within one batch dedup and
 * existence checks see staged rows exactly like stored ones. The pipeline's
 * closing validation is computed separately from the loop's tool-call audit
 * log (see [ExtractionPipeline]).
 *
 * ## `Long` epoch seconds
 *
 * The Python reference carries `mention_time` as a `float` epoch. This port
 * keeps the whole chain in [Long] epoch seconds (per the port-wide
 * convention), so [mentionTimeEpoch] is a [Long].
 */
object AgentTools {

    // Constants CONTENT_PREVIEW, ENTITY_SCAN_LIMIT, RRF_K are file-level
    // (below) so the nested ToolContext can reference them without qualification.

    /**
     * Assemble the 8 extraction tools bound to one ingest batch.
     *
     * @param store The read side; the only storage path for read tools.
     * @param tx The staging writer; every write tool records here, never in
     *   `store`.
     * @param embedder Embedding client for entity recall and node vectors.
     * @param registry Node type registry, validates `node_type` arguments.
     * @param mentionTimeEpoch Batch mention time as epoch seconds, injected
     *   into every staged node. Defaults to the current time.
     * @param batchMetadata Batch-level metadata merged into every staged
     *   content node (per-call metadata wins on key conflicts).
     * @return Tool name → [ToolDef], read tools first, then write tools.
     */
    fun buildAgentTools(
        store: MemStore,
        tx: TxWriter,
        embedder: EmbeddingClient,
        registry: NodeTypeRegistry,
        mentionTimeEpoch: Long = System.currentTimeMillis() / 1000L,
        batchMetadata: Map<String, Any?> = emptyMap(),
    ): Map<String, ToolDef> {
        val context =
            ToolContext(
                store = store,
                tx = tx,
                embedder = embedder,
                registry = registry,
                mentionTimeEpoch = mentionTimeEpoch,
                batchMetadata = batchMetadata.toMap(),
            )
        return linkedMapOf(
            "search_entities" to ToolDef(schema = searchEntitiesSchema(), handler = context::searchEntities),
            "get_entity" to ToolDef(schema = getEntitySchema(), handler = context::getEntity),
            "entity_events" to ToolDef(schema = entityEventsSchema(), handler = context::entityEvents),
            "register_entity" to ToolDef(schema = registerEntitySchema(), handler = context::registerEntity),
            "register_content" to ToolDef(schema = registerContentSchema(), handler = context::registerContent),
            "link_event_entity" to ToolDef(schema = linkEventEntitySchema(), handler = context::linkEventEntity),
            "update_entity_aliases" to ToolDef(schema = updateEntityAliasesSchema(), handler = context::updateEntityAliases),
            "supersede" to ToolDef(schema = supersedeSchema(), handler = context::supersede),
        )
    }

    // ------------------------------------------------------------------
    // Schemas (verbatim descriptions from agent_tools.py)
    // ------------------------------------------------------------------

    private fun searchEntitiesSchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "search_entities",
            description =
                "Search the entity registry: exact canonical/alias match plus " +
                    "dense-vector/BM25 hybrid recall. Call this for every name " +
                    "BEFORE registering; reuse a matching entity's hash instead " +
                    "of registering twice.",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "query" to mapOf("type" to "string", "description" to "name or alias to look up"),
                            "k" to mapOf("type" to "integer", "description" to "max vector-recall candidates (default 5)"),
                        ),
                    "required" to listOf("query"),
                ),
        )

    private fun getEntitySchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "get_entity",
            description = "Fetch one entity card in full (content + metadata).",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "hash_id" to mapOf("type" to "string", "description" to "the entity hash"),
                        ),
                    "required" to listOf("hash_id"),
                ),
        )

    private fun entityEventsSchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "entity_events",
            description =
                "List the events linked to an entity, newest mention first. " +
                    "Use it to check for outdated facts before supersede.",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "entity_hash" to mapOf("type" to "string", "description" to "the entity hash"),
                            "k" to mapOf("type" to "integer", "description" to "max events (default 10)"),
                        ),
                    "required" to listOf("entity_hash"),
                ),
        )

    private fun registerEntitySchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "register_entity",
            description =
                "Register a person/place/object/concept as an entity card. " +
                    "If the name already exists, the existing hash is returned " +
                    "with reused=true instead of creating a duplicate.",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "name" to mapOf("type" to "string", "description" to "canonical display name"),
                            "entity_type" to
                                mapOf(
                                    "type" to "string",
                                    "description" to "person | place | object | concept | ...",
                                ),
                            "description" to
                                mapOf(
                                    "type" to "string",
                                    "description" to "one line stating who/what this entity is " +
                                        "(factual, in the batch's language); becomes the card's " +
                                        "content after the name",
                                ),
                            "aliases" to
                                mapOf(
                                    "type" to "array",
                                    "items" to mapOf("type" to "string"),
                                    "description" to "alternative names (optional)",
                                ),
                        ),
                    "required" to listOf("name", "entity_type", "description"),
                ),
        )

    private fun registerContentSchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "register_content",
            description =
                "Register an event/life_capture (or any declared content " +
                    "type). entity_mentions must list every entity the content " +
                    "mentions; each must then be linked via link_event_entity or " +
                    "closing validation rejects the batch.",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "node_type" to mapOf("type" to "string", "description" to "e.g. event"),
                            "content" to mapOf("type" to "string", "description" to "one-line summary"),
                            "event_time_raw" to
                                mapOf(
                                    "type" to "string",
                                    "description" to "original time expression, verbatim",
                                ),
                            "event_time" to
                                mapOf(
                                    "type" to "string",
                                    "description" to "normalized time (see system prompt); " +
                                        "omit when unknown",
                                ),
                            "entity_mentions" to
                                mapOf(
                                    "type" to "array",
                                    "items" to mapOf("type" to "string"),
                                    "description" to "names of ALL entities this content mentions",
                                ),
                            "metadata" to
                                mapOf(
                                    "type" to "object",
                                    "description" to "type-specific fields (see node type spec)",
                                ),
                        ),
                    "required" to listOf("node_type", "content"),
                ),
        )

    private fun linkEventEntitySchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "link_event_entity",
            description =
                "Link a content node to an entity it mentions. Both hashes " +
                    "must come from earlier tool results; never invent them.",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "content_hash" to mapOf("type" to "string", "description" to "event/capture hash"),
                            "entity_hash" to mapOf("type" to "string", "description" to "entity hash"),
                            "role" to
                                mapOf(
                                    "type" to "string",
                                    "description" to "free-text role: 主角/地点/物品/承诺人...",
                                ),
                        ),
                    "required" to listOf("content_hash", "entity_hash"),
                ),
        )

    private fun updateEntityAliasesSchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "update_entity_aliases",
            description = "Add newly learned aliases to an entity (union; no duplicates).",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "entity_hash" to mapOf("type" to "string", "description" to "the entity hash"),
                            "add_aliases" to
                                mapOf(
                                    "type" to "array",
                                    "items" to mapOf("type" to "string"),
                                    "description" to "aliases to add",
                                ),
                        ),
                    "required" to listOf("entity_hash", "add_aliases"),
                ),
        )

    private fun supersedeSchema(): LlmToolDefinition =
        LlmToolDefinition(
            name = "supersede",
            description =
                "Mark an outdated content node as replaced by a newer one " +
                    "(both must be content nodes). Superseded nodes are hidden " +
                    "from retrieval by default.",
            parameters =
                mapOf(
                    "type" to "object",
                    "properties" to
                        mapOf(
                            "old_hash" to mapOf("type" to "string", "description" to "the outdated node"),
                            "new_hash" to mapOf("type" to "string", "description" to "its replacement"),
                        ),
                    "required" to listOf("old_hash", "new_hash"),
                ),
        )

    // ------------------------------------------------------------------
    // Shared helpers (mirrors agent_tools.py module-level helpers)
    // ------------------------------------------------------------------
    // These are file-level functions (below) so the nested ToolContext can
    // call them without qualifying with `AgentTools.`.

    /**
     * Mutable closure state shared by the 8 tool handlers.
     *
     * Mirrors the closure variables in `build_agent_tools`: the in-batch
     * staging view (`staged_entities`, `staged_content`, `supersede_marks`)
     * plus the injected batch context.
     */
    private class ToolContext(
        val store: MemStore,
        val tx: TxWriter,
        val embedder: EmbeddingClient,
        val registry: NodeTypeRegistry,
        val mentionTimeEpoch: Long,
        val batchMetadata: Map<String, Any?>,
    ) {
        // In-batch staging view: what the tools staged, keyed for fast lookup.
        val stagedEntities: MutableMap<String, MutableMap<String, Any?>> = LinkedHashMap()
        val stagedContent: MutableMap<String, MutableMap<String, Any?>> = LinkedHashMap()
        val supersedeMarks: MutableMap<String, String> = LinkedHashMap()

        // ------------------------------------------------------------------
        // Shared lookups over the staged view ∪ store
        // ------------------------------------------------------------------

        /**
         * Return the full entity card for a staged or stored entity, or `null`.
         *
         * The store is consulted first: after a flush it is authoritative
         * (`created_at`, persisted alias unions); the staged view covers
         * entities registered in this batch that are not flushed yet.
         */
        suspend fun entityCard(hashId: String): Map<String, Any?>? {
            val node = store.getNode(hashId)
            if (node != null && node.baseType == "entity") {
                return entityCardFromNode(node)
            }
            val staged = stagedEntities[hashId] ?: return null
            val card = LinkedHashMap(staged)
            card["mention_time"] = iso(mentionTimeEpoch) as Any?
            card["created_at"] = null
            return card
        }

        /** Return whether a live content node with this hash is staged or stored. */
        suspend fun contentExists(hashId: String): Boolean {
            if (hashId in stagedContent) return true
            val node = store.getNode(hashId)
            return node != null && node.baseType == "content"
        }

        /** Case-insensitive name/alias match against a staged entity card. */
        private fun entityNamesMatch(card: Map<String, Any?>, needle: String): Boolean {
            val names = mutableSetOf<String>()
            (card["canonical_name"] as? String)?.let { names.add(it.trim().lowercase(Locale.ROOT)) }
            (card["aliases"] as? List<*>)?.forEach { alias ->
                if (alias is String) names.add(alias.trim().lowercase(Locale.ROOT))
            }
            return needle in names
        }

        /** Embed one text into a plain float list. Mirrors `_embed`. */
        private suspend fun embed(text: String): List<Float> {
            val arr = embedder.embed(listOf(text))[0]
            return arr.toList()
        }

        // ------------------------------------------------------------------
        // Read tools
        // ------------------------------------------------------------------

        /** `search_entities`: exact name/alias match ∪ dense+BM25 hybrid recall. */
        @Suppress("UNCHECKED_CAST")
        suspend fun searchEntities(args: Map<String, Any?>): List<Map<String, Any?>> {
            noExtra(args - setOf("query", "k"))
            val query = reqStr(args["query"], "query")
            val k = if (args["k"] != null) positiveInt(args["k"], "k") else 5
            val results: MutableList<Map<String, Any?>> = ArrayList()
            val seen: MutableSet<String> = LinkedHashSet()

            suspend fun add(card: Map<String, Any?>) {
                val hash = card["hash"] as? String ?: return
                if (hash in seen) return
                seen.add(hash)
                results.add(entitySummary(card))
            }

            // Exact branch: stored canonical/alias match ∪ staged name match.
            for (node in store.findEntities(query)) {
                add(entityCardFromNode(node))
            }
            val needle = query.trim().lowercase(Locale.ROOT)
            for (card in stagedEntities.values) {
                if (entityNamesMatch(card, needle)) {
                    add(card)
                }
            }
            // Hybrid branch over stored entities: dense vectors fused with the
            // BM25 lexical index via reciprocal rank fusion. Exact hits above
            // always lead; the fused candidates follow, up to k per leg.
            val candidates: MutableList<String> = ArrayList()
            for (node in store.scanNodes(limit = ENTITY_SCAN_LIMIT)) {
                if (node.baseType == "entity") candidates.add(node.hashId)
            }
            if (candidates.isNotEmpty()) {
                val queryVec = embed(query).toFloatArray()
                val dense = store.vecTopK(queryVec, k, candidates.toSet())
                val lexical = store.entityLexTopK(query, k)
                val rrf: MutableMap<String, Double> = LinkedHashMap()
                for ((rank, pair) in dense.withIndex()) {
                    val hashId = pair.first
                    rrf[hashId] = (rrf[hashId] ?: 0.0) + 1.0 / (RRF_K + rank + 1)
                }
                for ((rank, pair) in lexical.withIndex()) {
                    val hashId = pair.first
                    rrf[hashId] = (rrf[hashId] ?: 0.0) + 1.0 / (RRF_K + rank + 1)
                }
                val sorted = rrf.entries.sortedWith(
                    compareByDescending<Map.Entry<String, Double>> { it.value }
                        .thenBy { it.key }
                )
                for ((hashId, _) in sorted) {
                    val node = store.getNode(hashId)
                    if (node != null) {
                        add(entityCardFromNode(node))
                    }
                }
            }
            return results
        }

        /** `get_entity`: return the full entity card (content + metadata). */
        suspend fun getEntity(args: Map<String, Any?>): Map<String, Any?> {
            noExtra(args - setOf("hash_id"))
            val hashId = reqStr(args["hash_id"], "hash_id")
            val card = entityCard(hashId)
                ?: throw ToolValidationError("get_entity: entity not found: '$hashId'")
            return card
        }

        /** `entity_events`: list an entity's events, newest mention first (superseded hidden). */
        suspend fun entityEvents(args: Map<String, Any?>): List<Map<String, Any?>> {
            noExtra(args - setOf("entity_hash", "k"))
            val entityHash = reqStr(args["entity_hash"], "entity_hash")
            val k = if (args["k"] != null) positiveInt(args["k"], "k") else 10
            if (entityCard(entityHash) == null) {
                throw ToolValidationError("entity_events: entity not found: '$entityHash'")
            }
            val events: MutableList<Node> = ArrayList()
            for (link in store.getLinks(entityHash = entityHash)) {
                val node = store.getNode(link.eventHash)
                if (node != null) events.add(node)
            }
            events.sortByDescending { it.mentionTime }
            return events.take(k).map { node ->
                mapOf(
                    "hash" to node.hashId,
                    "content" to truncate(node.content, CONTENT_PREVIEW),
                    "mention_time" to iso(node.mentionTime),
                )
            }
        }

        // ------------------------------------------------------------------
        // Write tools (stage onto tx, never into the store)
        // ------------------------------------------------------------------

        /** `register_entity`: register an entity card; an existing namesake is reused. */
        suspend fun registerEntity(args: Map<String, Any?>): Map<String, Any?> {
            noExtra(args - setOf("name", "entity_type", "description", "aliases"))
            val name = reqStr(args["name"], "name")
            val entityType = reqStr(args["entity_type"], "entity_type")
            val description = reqStr(args["description"], "description")
            val aliases = optStrList(args["aliases"], "aliases")
            // Dedup: stored entities first (authoritative), then this batch's.
            val existing = store.findEntities(name)
            if (existing.isNotEmpty()) {
                val node = existing.first()
                return mapOf(
                    "hash" to node.hashId,
                    "canonical_name" to (node.metadata["canonical_name"] as? String),
                    "reused" to true,
                )
            }
            val needle = name.trim().lowercase(Locale.ROOT)
            for (card in stagedEntities.values) {
                if (entityNamesMatch(card, needle)) {
                    return mapOf(
                        "hash" to card["hash"] as Any,
                        "canonical_name" to card["canonical_name"] as Any,
                        "reused" to true,
                    )
                }
            }
            val spec = registry.get("entity")
            val metadata: Map<String, Any?> =
                mapOf(
                    "canonical_name" to name,
                    "aliases" to aliases.toList(),
                    "entity_type" to entityType,
                    "appearance_count" to 0,
                )
            val content = "$name: $description"
            val hashId =
                tx.putNode(
                    nodeType = spec.nodeType,
                    content = content,
                    eventTime = null,
                    baseType = spec.baseType,
                    mentionTime = mentionTimeEpoch,
                    metadata = metadata,
                )
            tx.putVector(hashId, embed(content))
            stagedEntities[hashId] =
                linkedMapOf(
                    "hash" to hashId,
                    "canonical_name" to name,
                    "aliases" to aliases.toList(),
                    "entity_type" to entityType,
                    "appearance_count" to 0,
                    "content" to content,
                    "metadata" to metadata,
                )
            return mapOf("hash" to hashId, "canonical_name" to name, "reused" to false)
        }

        /** `register_content`: register an event/life_capture/declarative content node. */
        @Suppress("UNCHECKED_CAST")
        suspend fun registerContent(args: Map<String, Any?>): Map<String, Any?> {
            noExtra(args - setOf("node_type", "content", "event_time_raw", "event_time", "entity_mentions", "metadata"))
            val nodeType = reqStr(args["node_type"], "node_type")
            val spec = registry.get(nodeType)
            if (spec.baseType != "content") {
                throw ToolValidationError(
                    "register_content: '$nodeType' is an entity-base type; use register_entity instead"
                )
            }
            val content = reqStr(args["content"], "content")
            val eventTimeRaw = optStr(args["event_time_raw"], "event_time_raw")
            val eventTime = optStr(args["event_time"], "event_time")
            val mentions = optStrList(args["entity_mentions"], "entity_mentions")
            val metadata: Map<String, Any?> =
                if (args["metadata"] == null) {
                    emptyMap()
                } else {
                    val m = args["metadata"]
                    if (m !is Map<*, *>) {
                        throw ToolValidationError("metadata: expected an object, got ${repr(m)}")
                    }
                    @Suppress("UNCHECKED_CAST")
                    m as Map<String, Any?>
                }
            // event_time validity is checked by TimeParse.parseEventTime when
            // present; an invalid string is a validation failure fed back to
            // the model. The start_ts/end_ts expansion happens at flush time
            // (MemStore.applyPutNode), so here we only validate parseability.
            if (eventTime != null) {
                TimeParse.parseEventTime(eventTime)
            }
            val mergedMetadata: Map<String, Any?> =
                LinkedHashMap(batchMetadata).apply { putAll(metadata) }
            val hashId =
                tx.putNode(
                    nodeType = spec.nodeType,
                    content = content,
                    eventTime = eventTime,
                    baseType = "content",
                    mentionTime = mentionTimeEpoch,
                    eventTimeRaw = eventTimeRaw,
                    metadata = mergedMetadata,
                )
            tx.putVector(hashId, embed(content))
            stagedContent[hashId] =
                linkedMapOf(
                    "hash" to hashId,
                    "node_type" to spec.nodeType,
                    "entity_mentions" to mentions.toList(),
                )
            return mapOf("hash" to hashId, "node_type" to spec.nodeType)
        }

        /** `link_event_entity`: link a content node to an entity it mentions. */
        suspend fun linkEventEntity(args: Map<String, Any?>): Map<String, Any?> {
            noExtra(args - setOf("content_hash", "entity_hash", "role"))
            val contentHash = reqStr(args["content_hash"], "content_hash")
            val entityHash = reqStr(args["entity_hash"], "entity_hash")
            val role = optStr(args["role"], "role") ?: ""
            if (!contentExists(contentHash)) {
                throw ToolValidationError("link_event_entity: content node not found: '$contentHash'")
            }
            if (entityCard(entityHash) == null) {
                throw ToolValidationError("link_event_entity: entity not found: '$entityHash'")
            }
            val created = tx.putLink(contentHash, entityHash, role)
            if (created) {
                // +1 only for a newly created link: a repeated link of the same
                // (event, entity, role) triple bumps mention_count (W3), not the
                // entity's appearance_count (number of events linking to it).
                tx.updateNode(entityHash, mapOf("appearance_count" to 1))
                val staged = stagedEntities[entityHash]
                if (staged != null) {
                    val current = (staged["appearance_count"] as? Number)?.toInt() ?: 0
                    staged["appearance_count"] = current + 1
                }
            }
            return mapOf(
                "content_hash" to contentHash,
                "entity_hash" to entityHash,
                "role" to role,
                "created" to created,
            )
        }

        /** `update_entity_aliases`: union new aliases into an entity card. */
        @Suppress("UNCHECKED_CAST")
        suspend fun updateEntityAliases(args: Map<String, Any?>): Map<String, Any?> {
            noExtra(args - setOf("entity_hash", "add_aliases"))
            val entityHash = reqStr(args["entity_hash"], "entity_hash")
            val addAliases = reqStrList(args["add_aliases"], "add_aliases", allowEmpty = false)
            val card = entityCard(entityHash)
                ?: throw ToolValidationError("update_entity_aliases: entity not found: '$entityHash'")
            val existingAliases = (card["aliases"] as? List<*>) ?: emptyList<Any?>()
            val union: MutableList<String> = ArrayList()
            for (alias in existingAliases) {
                if (alias is String && alias !in union) union.add(alias)
            }
            for (alias in addAliases) {
                if (alias !in union) union.add(alias)
            }
            tx.updateNode(entityHash, mapOf("aliases" to addAliases))
            val staged = stagedEntities[entityHash]
            if (staged != null) {
                staged["aliases"] = union.toList()
            }
            return mapOf("hash" to entityHash, "aliases" to union.toList())
        }

        /** `supersede`: retire an outdated content node in favor of a newer one (改口). */
        suspend fun supersede(args: Map<String, Any?>): Map<String, Any?> {
            noExtra(args - setOf("old_hash", "new_hash"))
            val oldHash = reqStr(args["old_hash"], "old_hash")
            val newHash = reqStr(args["new_hash"], "new_hash")
            if (oldHash == newHash) {
                throw ToolValidationError("supersede: old_hash and new_hash must differ")
            }
            if (!contentExists(newHash)) {
                throw ToolValidationError("supersede: new content node not found: '$newHash'")
            }
            if (oldHash in supersedeMarks) {
                throw ToolValidationError("supersede: '$oldHash' already superseded in this batch")
            }
            if (oldHash !in stagedContent) {
                val node = store.getNode(oldHash, includeSuperseded = true)
                if (node == null || node.baseType != "content") {
                    throw ToolValidationError("supersede: old content node not found: '$oldHash'")
                }
                if (node.supersededBy != null) {
                    throw ToolValidationError("supersede: '$oldHash' is already superseded")
                }
            }
            tx.updateNode(oldHash, mapOf("superseded_by" to newHash))
            supersedeMarks[oldHash] = newHash
            return mapOf("old_hash" to oldHash, "new_hash" to newHash, "superseded" to true)
        }
    }
}

/**
 * Project a full entity card to the search-result summary fields.
 * Mirrors `_entity_summary` in `agent_tools.py`.
 */
private fun entitySummary(card: Map<String, Any?>): Map<String, Any?> =
    mapOf(
        "hash" to card["hash"],
        "canonical_name" to card["canonical_name"],
        "aliases" to ((card["aliases"] as? List<*>) ?: emptyList<Any?>()).toList(),
        "entity_type" to card["entity_type"],
        "appearance_count" to ((card["appearance_count"] as? Number)?.toInt() ?: 0),
    )

/**
 * Render a stored entity node as a full card dict.
 * Mirrors `_entity_card_from_node` in `agent_tools.py`.
 */
private fun entityCardFromNode(node: Node): Map<String, Any?> {
    val metadata = node.metadata
    return mapOf(
        "hash" to node.hashId,
        "canonical_name" to metadata["canonical_name"],
        "aliases" to ((metadata["aliases"] as? List<*>) ?: emptyList<Any?>()).toList(),
        "entity_type" to metadata["entity_type"],
        "appearance_count" to ((metadata["appearance_count"] as? Number)?.toInt() ?: 0),
        "content" to node.content,
        "metadata" to metadata,
                    "mention_time" to iso(node.mentionTime),
                    "created_at" to iso(node.createdAt.takeIf { it != 0L }),
    )
}

// ----------------------------------------------------------------------
// File-level constants (mirrors agent_tools.py module-level constants).
// File-level so the nested ToolContext can reference them without qualification.
// ----------------------------------------------------------------------

/** Content preview length in `entity_events` summaries. Mirrors `_CONTENT_PREVIEW`. */
private const val CONTENT_PREVIEW = 100

/** Upper bound when scanning for entity candidates for vector recall. Mirrors `_ENTITY_SCAN_LIMIT`. */
private const val ENTITY_SCAN_LIMIT = 100_000

/** Reciprocal-rank-fusion damping constant (the standard 60). Mirrors `_RRF_K`. */
private const val RRF_K = 60

// ----------------------------------------------------------------------
// File-level argument validation helpers (mirrors agent_tools.py module-level helpers).
// File-level so the nested ToolContext can call them without qualification.
// ----------------------------------------------------------------------

/** ISO 8601 formatter with second precision, no offset. */
private val AGENT_TOOLS_ISO_SECONDS: DateTimeFormatter =
    DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")

/** Reject unexpected keyword arguments. Mirrors `_no_extra`. */
private fun noExtra(extra: Map<String, Any?>) {
    if (extra.isNotEmpty()) {
        throw ToolValidationError("unexpected arguments: ${extra.keys.sorted()}")
    }
}

/** Require a non-empty string argument. Mirrors `_req_str`. */
private fun reqStr(value: Any?, name: String): String {
    if (value !is String || value.isBlank()) {
        throw ToolValidationError("$name: expected a non-empty string, got ${repr(value)}")
    }
    return value
}

/** Optional string; empty/whitespace strings read as absent. Mirrors `_opt_str`. */
private fun optStr(value: Any?, name: String): String? {
    if (value == null) return null
    if (value !is String) {
        throw ToolValidationError("$name: expected a string, got ${repr(value)}")
    }
    return value.takeIf { it.isNotBlank() }
}

/** Require a list of non-empty strings. Mirrors `_req_str_list`. */
private fun reqStrList(
    value: Any?,
    name: String,
    allowEmpty: Boolean = true,
): List<String> {
    if (value !is List<*>) {
        throw ToolValidationError("$name: expected a list of non-empty strings, got ${repr(value)}")
    }
    for (item in value) {
        if (item !is String || item.isBlank()) {
            throw ToolValidationError("$name: expected a list of non-empty strings, got ${repr(value)}")
        }
    }
    if (!allowEmpty && value.isEmpty()) {
        throw ToolValidationError("$name: must not be empty")
    }
    return value.map { (it as String).trim() }
}

/** Optional string list; `null` reads as `[]`. Mirrors `_opt_str_list`. */
private fun optStrList(value: Any?, name: String): List<String> {
    if (value == null) return emptyList()
    return reqStrList(value, name)
}

/** Require a positive integer (bools rejected). Mirrors `_positive_int`. */
private fun positiveInt(value: Any?, name: String): Int {
    if (value !is Int || value < 1) {
        throw ToolValidationError("$name: expected a positive integer, got ${repr(value)}")
    }
    return value
}

/** Format epoch seconds as local-naive ISO 8601 (second precision). Mirrors `_iso`. */
private fun iso(epoch: Long?, zoneId: ZoneId = ZoneId.systemDefault()): String? {
    if (epoch == null) return null
    return ZonedDateTime.ofInstant(Instant.ofEpochSecond(epoch), zoneId)
        .format(AGENT_TOOLS_ISO_SECONDS)
}

/** Truncate to [limit] chars, marking truncation with `[+]`. Mirrors `_truncate`. */
private fun truncate(text: String, limit: Int): String =
    if (text.length > limit) text.substring(0, limit) + "[+]" else text

/** Python-style `repr` for error messages. */
private fun repr(value: Any?): String =
    when (value) {
        null -> "None"
        is String -> "'$value'"
        else -> value.toString()
    }
