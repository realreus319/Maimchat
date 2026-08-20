package com.l2dchat.core.mem

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * Room entities for the mem four-table schema (design section 4 / `store.py` `_SCHEMA`).
 *
 * Each table is prefixed `mem_` so the new schema is disjoint from the legacy
 * `memories` table (which is dropped in the v4→v5 destructive migration — see
 * `ChatDatabase.MIGRATION_4_5`).
 *
 * ## Type mapping (Python `_SCHEMA` → Kotlin/Room)
 *
 * | Python column type | Kotlin type | SQLite affinity |
 * | --- | --- | --- |
 * | `TEXT`            | `String` / `String?`  | TEXT    |
 * | `INT` / `INTEGER` | `Int` / `Long`        | INTEGER |
 * | `REAL`            | `Long` (epoch seconds) / `Double?` | INTEGER / REAL |
 * | `BLOB`            | `ByteArray`           | BLOB    |
 *
 * The Python reference stores epoch seconds as `float`/`REAL`; this port uses
 * [Long] throughout (per the mem-kotlin-integration plan) so the whole chain
 * stays in integer epoch seconds. [Double] is only used for the model-assigned
 * `importance`/`sentiment` scores, which are genuinely floating-point.
 *
 * These entities are defined but NOT yet registered in `ChatDatabase` —
 * registration happens in wave B (T1.3). Room still validates the `@Entity`
 * annotations at KSP compile time of this module, which is independent of
 * `@Database` registration.
 */

/**
 * A single memory node — an event, an entity registry card, or a life capture.
 *
 * Schema mirrors `store.py` `CREATE TABLE nodes`. The table is renamed to
 * `mem_nodes` to avoid confusion with the legacy `memories` table.
 *
 * - Primary key: [hashId] (content-addressed 12-char id).
 * - Indices on `node_type`, `start_ts`, `mention_time` (matches the three
 *   `CREATE INDEX` statements in `_SCHEMA`).
 */
@Entity(
        tableName = "mem_nodes",
        indices = [Index("node_type"), Index("start_ts"), Index("mention_time")]
)
data class MemNodeEntity(
        @PrimaryKey @ColumnInfo(name = "hash_id") val hashId: String,
        @ColumnInfo(name = "base_type") val baseType: String,
        @ColumnInfo(name = "node_type") val nodeType: String,
        @ColumnInfo(name = "content") val content: String,
        @ColumnInfo(name = "mention_time") val mentionTime: Long,
        @ColumnInfo(name = "event_time_raw") val eventTimeRaw: String?,
        @ColumnInfo(name = "event_time") val eventTime: String?,
        @ColumnInfo(name = "start_ts") val startTs: Long?,
        @ColumnInfo(name = "end_ts") val endTs: Long?,
        @ColumnInfo(name = "importance") val importance: Double?,
        @ColumnInfo(name = "sentiment") val sentiment: Double?,
        @ColumnInfo(name = "metadata_json") val metadataJson: String,
        @ColumnInfo(name = "superseded_by") val supersededBy: String?,
        @ColumnInfo(name = "access_count") val accessCount: Int,
        @ColumnInfo(name = "last_accessed") val lastAccessed: Long?,
        @ColumnInfo(name = "created_at") val createdAt: Long,
        @ColumnInfo(name = "updated_at") val updatedAt: Long
)

/**
 * Directed edge from an event node to an entity node, with a role label.
 *
 * Schema mirrors `store.py` `CREATE TABLE event_entity_links`. The table is
 * renamed to `mem_event_entity_links`.
 *
 * - Composite primary key: `(event_hash, entity_hash, role)` — a given
 *   (event, entity, role) triple is unique; re-mentioning the same edge
 *   increments `mention_count` (see [MemLinkDao.upsertLink]).
 * - Index on `entity_hash` (matches `idx_links_entity_hash`).
 */
@Entity(
        tableName = "mem_event_entity_links",
        primaryKeys = ["event_hash", "entity_hash", "role"],
        indices = [Index("entity_hash")]
)
data class MemEventEntityLinkEntity(
        @ColumnInfo(name = "event_hash") val eventHash: String,
        @ColumnInfo(name = "entity_hash") val entityHash: String,
        @ColumnInfo(name = "role") val role: String,
        @ColumnInfo(name = "mention_count") val mentionCount: Int = 1,
        @ColumnInfo(name = "created_at") val createdAt: Long
)

/**
 * A pre-computed embedding vector for a node, stored as a raw float blob.
 *
 * Schema mirrors `store.py` `CREATE TABLE vectors`. The table is renamed to
 * `mem_vectors`.
 *
 * - Primary key: [hashId] (1:1 with [MemNodeEntity.hashId]).
 * - [embedding] is a [ByteArray] of `dim * 4` bytes (little-endian IEEE 754),
 *   matching how [VectorIndex] decodes it via `ByteBuffer.asFloatBuffer()`.
 *
 * ### `equals` / `hashCode`
 *
 * [ByteArray] uses reference identity by default, which breaks data-class
 * equality semantics (two rows with identical blob contents would compare
 * unequal). Both are overridden to use [ByteArray.contentEquals] /
 * [ByteArray.contentHashCode] so the entity behaves as a proper value object.
 */
@Entity(tableName = "mem_vectors")
data class MemVectorEntity(
        @PrimaryKey @ColumnInfo(name = "hash_id") val hashId: String,
        @ColumnInfo(name = "dim") val dim: Int,
        @ColumnInfo(name = "embedding") val embedding: ByteArray
) {
    override fun equals(other: Any?): Boolean =
            this === other ||
                    (other is MemVectorEntity &&
                            hashId == other.hashId &&
                            dim == other.dim &&
                            embedding.contentEquals(other.embedding))

    override fun hashCode(): Int = 31 * hashId.hashCode() + embedding.contentHashCode()
}

/**
 * Idempotency receipt for an ingest batch (design section 2 replay safety).
 *
 * Schema mirrors `store.py` `CREATE TABLE receipts`. The table is renamed to
 * `mem_receipts`.
 *
 * - Primary key: [batchId].
 * - A row with `status = "done"` makes re-applying the same batch a no-op
 *   (see [MemReceiptDao.findDoneBatchIds] and `MemStore.apply`).
 */
@Entity(tableName = "mem_receipts")
data class MemReceiptEntity(
        @PrimaryKey @ColumnInfo(name = "batch_id") val batchId: String,
        @ColumnInfo(name = "source") val source: String?,
        @ColumnInfo(name = "status") val status: String,
        @ColumnInfo(name = "node_hashes_json") val nodeHashesJson: String,
        @ColumnInfo(name = "link_count") val linkCount: Int,
        @ColumnInfo(name = "error") val error: String?,
        @ColumnInfo(name = "created_at") val createdAt: Long
)
