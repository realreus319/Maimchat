package com.l2dchat.core.mem

/**
 * Ingest batch contract and batch receipts (design doc sections 0/2).
 *
 * Mirrors `mem/batch.py` from the Python reference. [IngestBatch] is the
 * library's single input contract: the host assembles a text batch (a compacted
 * conversation window, a media digest, a manual note), optionally with image
 * attachments, and hands it to the extraction pipeline. [Receipt] is what the
 * pipeline returns for every batch — the idempotency key ([batchId]), the
 * outcome status, and the hashes of everything written.
 *
 * All time fields are epoch seconds as [Long] (never [Double]/[Float]), per the
 * port-wide convention documented in the integration plan.
 */

/**
 * Allowed [IngestBatch.source] values (design section 0).
 *
 * The [wireValue] of each constant matches the Python `SOURCES` tuple exactly so
 * that persisted values stay stable across the port.
 *
 * @property wireValue The string stored on the wire and in the receipts table.
 */
enum class BatchSource(val wireValue: String) {
    CHAT_COMPACT("chat_compact"),
    LIFE_CAPTURE("life_capture"),
    MANUAL("manual"),
    BACKFILL("backfill"),
}

/**
 * Receipt status values (design section 4 receipts table).
 *
 * @property wireValue The string stored in the receipts table.
 */
enum class ReceiptStatus(val wireValue: String) {
    DONE("done"),
    FAILED("failed"),
}

/**
 * One image file attached to an ingest batch (design section 0).
 *
 * @property path Host-side image file path; read and inlined as a base64 data
 *   URL when the user message for the extraction model is built.
 * @property placeholder Literal string in [IngestBatch.text] marking where the
 *   image belongs (e.g. `"[图片]"`). When it occurs in the text the image is
 *   inserted in place; otherwise (`null` or no hit) the image is appended after
 *   the text.
 */
data class ImageAttachment(
    val path: String,
    val placeholder: String? = null,
)

/**
 * One text batch to ingest (design section 0 input contract).
 *
 * @property batchId Host-generated idempotency key, e.g.
 *   `"compact_{context_id}_{upto_msg_id}"`.
 * @property source One of [BatchSource].
 * @property text Plain text; conversation batches should carry `[HH:MM speaker]`
 *   line prefixes.
 * @property timeAnchor Anchor for resolving relative time expressions (the
 *   batch's newest message time), as epoch seconds.
 * @property mentionTime "Appearing when" — when this batch was said/recorded,
 *   as epoch seconds.
 * @property metadata Source-related extras (life_capture batches carry
 *   modality/media_path/captured_at here).
 * @property images Image attachments sent to the multimodal extraction model
 *   alongside the text.
 * @throws IllegalArgumentException if [batchId] or [text] is blank.
 */
data class IngestBatch(
    val batchId: String,
    val source: BatchSource,
    val text: String,
    val timeAnchor: Long,
    val mentionTime: Long,
    val metadata: Map<String, Any?> = emptyMap(),
    val images: List<ImageAttachment> = emptyList(),
) {
    init {
        require(batchId.isNotBlank()) { "IngestBatch: batch_id must be a non-empty string" }
        require(text.isNotBlank()) { "IngestBatch: text must be a non-empty string" }
    }
}

/**
 * Outcome of ingesting one batch (design sections 2/4).
 *
 * @property batchId The ingested batch's idempotency key.
 * @property source The batch's source (raw wire value, e.g. `"chat_compact"`).
 * @property status [ReceiptStatus.DONE] when the batch committed,
 *   [ReceiptStatus.FAILED] otherwise (the [error] field then carries the reason).
 * @property nodeHashes Hashes of the nodes this batch wrote (done only).
 * @property linkCount Number of event-entity links this batch created.
 * @property error Failure reason, or `null` on success.
 * @property createdAt Receipt creation epoch seconds.
 * @property stats Run statistics — turns, tool_calls, validation_retries and the
 *   accumulated LLM token `usage`. Not persisted in the receipts table (the
 *   section-4 DDL has no column for it), so a receipt rebuilt from the store
 *   carries empty stats.
 */
data class Receipt(
    val batchId: String,
    val source: String,
    val status: ReceiptStatus,
    val nodeHashes: List<String> = emptyList(),
    val linkCount: Int = 0,
    val error: String? = null,
    val createdAt: Long = 0L,
    val stats: Map<String, Any?> = emptyMap(),
) {
    /**
     * Serialize for the receipts table write atom (W5).
     *
     * [stats] is dropped because the section-4 DDL has no column for it.
     */
    fun toStoreDict(): Map<String, Any?> =
        mapOf(
            "batch_id" to batchId,
            "source" to source,
            "status" to status.wireValue,
            "node_hashes" to nodeHashes.toList(),
            "link_count" to linkCount,
            "error" to error,
            "created_at" to createdAt,
        )

    companion object {
        /**
         * Rebuild a receipt from the store's receipt dict (read atom R6).
         *
         * [stats] stays empty (never persisted). Unknown status wire values map
         * to [ReceiptStatus.FAILED] defensively, matching the Python reference's
         * `str(data["status"])` round-trip expectation that the store only ever
         * returns persisted values.
         */
        fun fromStoreDict(data: Map<String, Any?>): Receipt {
            val statusWire = data["status"] as? String
            val status = ReceiptStatus.entries.firstOrNull { it.wireValue == statusWire }
                ?: ReceiptStatus.FAILED
            val rawHashes = data["node_hashes"] as? Iterable<*>
            return Receipt(
                batchId = data["batch_id"] as? String ?: "",
                source = data["source"] as? String ?: "",
                status = status,
                nodeHashes = rawHashes?.map { it?.toString().orEmpty() } ?: emptyList(),
                linkCount = (data["link_count"] as? Number)?.toInt() ?: 0,
                error = data["error"] as? String,
                createdAt = (data["created_at"] as? Number)?.toLong() ?: 0L,
                stats = emptyMap(),
            )
        }
    }
}
