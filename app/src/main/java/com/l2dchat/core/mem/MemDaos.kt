package com.l2dchat.core.mem

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

/**
 * Room DAOs for the mem four-table schema.
 *
 * The methods cover the read atoms R1–R8 and the write atoms W1–W6 from the
 * mem design (see `store.py` `MemStore`). Every storage path in `MemStore`
 * goes through these DAOs — nothing bypasses them.
 *
 * Defined here but NOT yet wired into `ChatDatabase` — registration happens in
 * wave B (T1.3). Room still validates the `@Dao` SQL at KSP compile time.
 */

/* ------------------------------------------------------------------ */
/* mem_nodes                                                           */
/* ------------------------------------------------------------------ */

@Dao
interface MemNodeDao {
    /**
     * W1 — `INSERT OR IGNORE`. A content-addressed hash collision must NOT
     * overwrite the existing row (the first writer wins; later writes are
     * dropped silently). Returns no row count because [OnConflictStrategy.IGNORE]
     * makes the insert a no-op on PK conflict.
     */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertOrIgnore(node: MemNodeEntity)

    /** W2 — replace the `metadata_json` whitelist field and bump `updated_at`. */
    @Query(
            """
            UPDATE mem_nodes
            SET metadata_json = :json,
                updated_at = :now
            WHERE hash_id = :hashId
            """
    )
    suspend fun updateMetadata(hashId: String, json: String, now: Long)

    /** Increment `access_count` by [delta] and stamp `last_accessed`. */
    @Query(
            """
            UPDATE mem_nodes
            SET access_count = access_count + :delta,
                last_accessed = :now
            WHERE hash_id = :hashId
            """
    )
    suspend fun incrementAccess(hashId: String, delta: Int, now: Long)

    /** Mark this node superseded by [by] (or clear it with `null`). */
    @Query(
            """
            UPDATE mem_nodes
            SET superseded_by = :by,
                updated_at = :now
            WHERE hash_id = :hashId
            """
    )
    suspend fun setSuperseded(hashId: String, by: String?, now: Long)

    /** R1 — single-node lookup by full hash id. */
    @Query("SELECT * FROM mem_nodes WHERE hash_id = :hashId")
    suspend fun getById(hashId: String): MemNodeEntity?

    /**
     * R2 — every live (non-superseded) entity node. The Python reference also
     * full-scans entities (the entity set is small), so no LIMIT.
     */
    @Query(
            """
            SELECT * FROM mem_nodes
            WHERE base_type = 'entity' AND superseded_by IS NULL
            """
    )
    suspend fun getAllLiveEntities(): List<MemNodeEntity>

    /**
     * R3 (a) — live nodes of a given type, newest-mention first.
     *
     * Room cannot build a dynamic `WHERE` clause, so the Python `scan_nodes`
     * combinator is split into [scanByType] / [scanByMentionRange] /
     * [scanByTimeOverlap] / [scanByEntityHash].
     */
    @Query(
            """
            SELECT * FROM mem_nodes
            WHERE node_type = :nodeType AND superseded_by IS NULL
            ORDER BY mention_time DESC
            LIMIT :limit
            """
    )
    suspend fun scanByType(nodeType: String, limit: Int): List<MemNodeEntity>

    /** R3 (b) — live nodes whose `mention_time` falls in `[start, end)`. */
    @Query(
            """
            SELECT * FROM mem_nodes
            WHERE mention_time >= :start AND mention_time < :end
              AND superseded_by IS NULL
            ORDER BY mention_time DESC
            LIMIT :limit
            """
    )
    suspend fun scanByMentionRange(start: Long, end: Long, limit: Int): List<MemNodeEntity>

    /**
     * R3 (c) — live nodes whose `[start_ts, end_ts)` half-open interval
     * overlaps the query half-open interval `[qStart, qEnd)`.
     *
     * Two half-open intervals `[a, b)` and `[c, d)` overlap iff
     * `a < d AND c < b` — here `start_ts < qEnd AND end_ts > qStart`.
     * Adjacent (touching-but-not-overlapping) intervals do NOT match, matching
     * the Python `scan_nodes(time_overlap=...)` semantics.
     */
    @Query(
            """
            SELECT * FROM mem_nodes
            WHERE start_ts < :qEnd AND end_ts > :qStart
              AND superseded_by IS NULL
            ORDER BY mention_time DESC
            LIMIT :limit
            """
    )
    suspend fun scanByTimeOverlap(qStart: Long, qEnd: Long, limit: Int): List<MemNodeEntity>

    /**
     * R3 (d) — live event nodes linked to a given entity, via the
     * `mem_event_entity_links` join table.
     */
    @Query(
            """
            SELECT n.* FROM mem_nodes n
            INNER JOIN mem_event_entity_links l
                ON l.event_hash = n.hash_id
            WHERE l.entity_hash = :entityHash
              AND n.superseded_by IS NULL
            ORDER BY n.mention_time DESC
            LIMIT :limit
            """
    )
    suspend fun scanByEntityHash(entityHash: String, limit: Int): List<MemNodeEntity>

    /**
     * R3 (e) — every node row, live AND superseded, newest-mention first.
     *
     * Unlike [scanByType] / [scanByMentionRange] / [scanByTimeOverlap] /
     * [scanByEntityHash], this query does NOT filter `superseded_by IS NULL`,
     * so superseded rows are returned too. It backs the transitive
     * predecessor walk in `MemRetrieval._predecessors` (which scans every
     * node including superseded ones and follows `superseded_by` pointers),
     * matching the Python `scan_nodes(include_superseded=True)` no-filter
     * branch.
     */
    @Query(
            """
            SELECT * FROM mem_nodes
            ORDER BY mention_time DESC
            LIMIT :limit
            """
    )
    suspend fun scanAll(limit: Int): List<MemNodeEntity>

    /**
     * R8 — resolve a short hash prefix to zero or more full hash ids.
     *
     * The `LIKE :prefix || '%'` pattern anchors at the start of `hash_id` and
     * matches any continuation, so `"abc"` → `"abc…"`. Ordered by `hash_id`
     * for deterministic resolution.
     */
    @Query(
            """
            SELECT hash_id FROM mem_nodes
            WHERE hash_id LIKE :prefix || '%'
            ORDER BY hash_id
            LIMIT :limit
            """
    )
    suspend fun resolveHashPrefix(prefix: String, limit: Int): List<String>

    /** W6 — hard-delete a single node. Returns the number of rows deleted (0 or 1). */
    @Query("DELETE FROM mem_nodes WHERE hash_id = :hashId")
    suspend fun deleteById(hashId: String): Int
}

/* ------------------------------------------------------------------ */
/* mem_event_entity_links                                              */
/* ------------------------------------------------------------------ */

@Dao
interface MemLinkDao {
    /**
     * W3 — upsert a directed event→entity edge.
     *
     * On PK conflict `(event_hash, entity_hash, role)` the existing
     * `mention_count` is incremented by the incoming value (NOT replaced),
     * matching the Python `INSERT ... ON CONFLICT(...) DO UPDATE SET
     * mention_count = mention_count + excluded.mention_count`.
     */
    @Query(
            """
            INSERT INTO mem_event_entity_links(
                event_hash, entity_hash, role, mention_count, created_at)
            VALUES(:eventHash, :entityHash, :role, :mentionCount, :createdAt)
            ON CONFLICT(event_hash, entity_hash, role) DO UPDATE SET
                mention_count = mention_count + excluded.mention_count
            """
    )
    suspend fun upsertLink(
            eventHash: String,
            entityHash: String,
            role: String,
            mentionCount: Int,
            createdAt: Long
    )

    /** R5 (a) — every edge originating from [eventHash]. */
    @Query("SELECT * FROM mem_event_entity_links WHERE event_hash = :eventHash")
    suspend fun linksOfEvent(eventHash: String): List<MemEventEntityLinkEntity>

    /** R5 (b) — every edge pointing at [entityHash]. */
    @Query("SELECT * FROM mem_event_entity_links WHERE entity_hash = :entityHash")
    suspend fun linksOfEntity(entityHash: String): List<MemEventEntityLinkEntity>

    /**
     * W6 cascade — drop every edge that references [hashId] on either side.
     * Used when a node is deleted so no dangling edge remains.
     */
    @Query(
            """
            DELETE FROM mem_event_entity_links
            WHERE event_hash = :hashId OR entity_hash = :hashId
            """
    )
    suspend fun deleteByHash(hashId: String)
}

/* ------------------------------------------------------------------ */
/* mem_vectors                                                         */
/* ------------------------------------------------------------------ */

@Dao
interface MemVectorDao {
    /** W4 — `INSERT OR REPLACE` (re-embedding a node overwrites the old vector). */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(vector: MemVectorEntity)

    /** Bulk-load every vector at startup so [VectorIndex] can warm its in-memory index. */
    @Query("SELECT * FROM mem_vectors")
    suspend fun loadAll(): List<MemVectorEntity>

    /** W6 — drop a vector when its owning node is deleted. */
    @Query("DELETE FROM mem_vectors WHERE hash_id = :hashId")
    suspend fun deleteById(hashId: String)
}

/* ------------------------------------------------------------------ */
/* mem_receipts                                                        */
/* ------------------------------------------------------------------ */

@Dao
interface MemReceiptDao {
    /** W5 — `INSERT OR REPLACE` (a re-applied batch overwrites its prior receipt). */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(receipt: MemReceiptEntity)

    /** R6 — fetch a single batch's receipt, or `null` if never recorded. */
    @Query("SELECT * FROM mem_receipts WHERE batch_id = :batchId")
    suspend fun getById(batchId: String): MemReceiptEntity?

    /**
     * Idempotency probe — which of [batchIds] already have a `done` receipt?
     * Used by `MemStore.apply` to short-circuit replay of an already-applied
     * batch (design section 2).
     */
    @Query(
            """
            SELECT batch_id FROM mem_receipts
            WHERE status = 'done' AND batch_id IN (:batchIds)
            """
    )
    suspend fun findDoneBatchIds(batchIds: List<String>): List<String>
}
