package com.l2dchat.core.mem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Mirrors `mem/batch.py` from the Python reference.
 *
 * Covers [IngestBatch] validation, the four [BatchSource] wire values, the two
 * [ReceiptStatus] values, and [Receipt.toStoreDict]/[Receipt.fromStoreDict]
 * round-trip semantics (with `stats` dropped on rebuild).
 */
class BatchTest {
    // ----- IngestBatch validation -------------------------------------------------

    @Test
    fun `IngestBatch accepts a well-formed chat_compact batch`() {
        val batch =
            IngestBatch(
                batchId = "compact_ctx1_42",
                source = BatchSource.CHAT_COMPACT,
                text = "[15:30 user] hello",
                timeAnchor = 1_723_000_000L,
                mentionTime = 1_723_000_000L,
            )
        assertEquals("compact_ctx1_42", batch.batchId)
        assertEquals(BatchSource.CHAT_COMPACT, batch.source)
        assertEquals("[15:30 user] hello", batch.text)
        assertEquals(1_723_000_000L, batch.timeAnchor)
        assertEquals(1_723_000_000L, batch.mentionTime)
        assertTrue("metadata must default to empty", batch.metadata.isEmpty())
        assertTrue("images must default to empty", batch.images.isEmpty())
    }

    @Test
    fun `IngestBatch rejects a blank batchId with IllegalArgumentException`() {
        try {
            IngestBatch(
                batchId = "   ",
                source = BatchSource.MANUAL,
                text = "note",
                timeAnchor = 0L,
                mentionTime = 0L,
            )
            fail("Expected IllegalArgumentException for blank batchId")
        } catch (e: IllegalArgumentException) {
            assertTrue(
                "Message must mention batch_id: ${e.message}",
                e.message?.contains("batch_id") == true,
            )
        }
    }

    @Test
    fun `IngestBatch rejects an empty batchId with IllegalArgumentException`() {
        try {
            IngestBatch(
                batchId = "",
                source = BatchSource.MANUAL,
                text = "note",
                timeAnchor = 0L,
                mentionTime = 0L,
            )
            fail("Expected IllegalArgumentException for empty batchId")
        } catch (e: IllegalArgumentException) {
            assertTrue(
                "Message must mention batch_id: ${e.message}",
                e.message?.contains("batch_id") == true,
            )
        }
    }

    @Test
    fun `IngestBatch rejects a blank text with IllegalArgumentException`() {
        try {
            IngestBatch(
                batchId = "b1",
                source = BatchSource.MANUAL,
                text = "   ",
                timeAnchor = 0L,
                mentionTime = 0L,
            )
            fail("Expected IllegalArgumentException for blank text")
        } catch (e: IllegalArgumentException) {
            assertTrue(
                "Message must mention text: ${e.message}",
                e.message?.contains("text") == true,
            )
        }
    }

    @Test
    fun `IngestBatch rejects an empty text with IllegalArgumentException`() {
        try {
            IngestBatch(
                batchId = "b1",
                source = BatchSource.MANUAL,
                text = "",
                timeAnchor = 0L,
                mentionTime = 0L,
            )
            fail("Expected IllegalArgumentException for empty text")
        } catch (e: IllegalArgumentException) {
            assertTrue(
                "Message must mention text: ${e.message}",
                e.message?.contains("text") == true,
            )
        }
    }

    @Test
    fun `IngestBatch preserves metadata and images when supplied`() {
        val images =
            listOf(
                ImageAttachment("/tmp/a.png", "[图片]"),
                ImageAttachment("/tmp/b.jpg"),
            )
        val batch =
            IngestBatch(
                batchId = "life_1",
                source = BatchSource.LIFE_CAPTURE,
                text = "captured moment",
                timeAnchor = 100L,
                mentionTime = 200L,
                metadata = mapOf("modality" to "image", "media_path" to "/tmp/a.png"),
                images = images,
            )
        assertEquals(2, batch.metadata.size)
        assertEquals("image", batch.metadata["modality"])
        assertEquals("/tmp/a.png", batch.metadata["media_path"])
        assertEquals(2, batch.images.size)
        assertEquals("[图片]", batch.images[0].placeholder)
        assertNull(batch.images[1].placeholder)
    }

    // ----- BatchSource wire values ------------------------------------------------

    @Test
    fun `BatchSource has exactly four constants`() {
        assertEquals(4, BatchSource.entries.size)
    }

    @Test
    fun `BatchSource CHAT_COMPACT wire value is chat_compact`() {
        assertEquals("chat_compact", BatchSource.CHAT_COMPACT.wireValue)
    }

    @Test
    fun `BatchSource LIFE_CAPTURE wire value is life_capture`() {
        assertEquals("life_capture", BatchSource.LIFE_CAPTURE.wireValue)
    }

    @Test
    fun `BatchSource MANUAL wire value is manual`() {
        assertEquals("manual", BatchSource.MANUAL.wireValue)
    }

    @Test
    fun `BatchSource BACKFILL wire value is backfill`() {
        assertEquals("backfill", BatchSource.BACKFILL.wireValue)
    }

    @Test
    fun `BatchSource wire values match the Python SOURCES tuple exactly`() {
        val wireValues = BatchSource.entries.map { it.wireValue }
        assertEquals(
            "Order and spelling must match Python SOURCES",
            listOf("chat_compact", "life_capture", "manual", "backfill"),
            wireValues,
        )
    }

    // ----- ReceiptStatus ----------------------------------------------------------

    @Test
    fun `ReceiptStatus has exactly two constants`() {
        assertEquals(2, ReceiptStatus.entries.size)
    }

    @Test
    fun `ReceiptStatus DONE wire value is done`() {
        assertEquals("done", ReceiptStatus.DONE.wireValue)
    }

    @Test
    fun `ReceiptStatus FAILED wire value is failed`() {
        assertEquals("failed", ReceiptStatus.FAILED.wireValue)
    }

    @Test
    fun `ReceiptStatus wire values match the Python STATUS constants exactly`() {
        val wireValues = ReceiptStatus.entries.map { it.wireValue }
        assertEquals(listOf("done", "failed"), wireValues)
    }

    // ----- ImageAttachment --------------------------------------------------------

    @Test
    fun `ImageAttachment default placeholder is null`() {
        val attachment = ImageAttachment("/tmp/img.png")
        assertEquals("/tmp/img.png", attachment.path)
        assertNull(attachment.placeholder)
    }

    @Test
    fun `ImageAttachment preserves an explicit placeholder`() {
        val attachment = ImageAttachment("/tmp/img.png", "[图片]")
        assertEquals("/tmp/img.png", attachment.path)
        assertEquals("[图片]", attachment.placeholder)
    }

    // ----- Receipt toStoreDict / fromStoreDict -----------------------------------

    @Test
    fun `Receipt toStoreDict includes all persisted fields and drops stats`() {
        val receipt =
            Receipt(
                batchId = "b1",
                source = "chat_compact",
                status = ReceiptStatus.DONE,
                nodeHashes = listOf("h1", "h2"),
                linkCount = 3,
                error = null,
                createdAt = 1_700_000_000L,
                stats = mapOf("turns" to 5, "tokens" to 1234),
            )
        val dict = receipt.toStoreDict()
        assertEquals("b1", dict["batch_id"])
        assertEquals("chat_compact", dict["source"])
        assertEquals("done", dict["status"])
        assertEquals(listOf("h1", "h2"), dict["node_hashes"])
        assertEquals(3, dict["link_count"])
        assertNull(dict["error"])
        assertEquals(1_700_000_000L, dict["created_at"])
        assertFalse(
            "stats must NOT be persisted in toStoreDict",
            dict.containsKey("stats"),
        )
    }

    @Test
    fun `Receipt toStoreDict status is the wire value, not the enum name`() {
        val done = Receipt("b", "manual", ReceiptStatus.DONE).toStoreDict()
        val failed = Receipt("b", "manual", ReceiptStatus.FAILED).toStoreDict()
        assertEquals("done", done["status"])
        assertEquals("failed", failed["status"])
    }

    @Test
    fun `Receipt toStoreDict node_hashes is a fresh list, not the source reference`() {
        val sourceHashes: MutableList<String> = mutableListOf("h1")
        val receipt =
            Receipt(
                batchId = "b",
                source = "manual",
                status = ReceiptStatus.DONE,
                nodeHashes = sourceHashes,
            )
        val dict = receipt.toStoreDict()
        val stored = dict["node_hashes"]
        assertTrue(
            "node_hashes must be serialized as some kind of list: got ${stored?.javaClass}",
            stored is List<*>,
        )
        // Mutating the original mutable list must not affect the serialized form.
        sourceHashes.add("hX")
        assertEquals(
            "Mutating the source list must not leak into the serialized form",
            listOf("h1"),
            stored,
        )
    }

    @Test
    fun `Receipt fromStoreDict round-trips a done receipt without stats`() {
        val original =
            Receipt(
                batchId = "b1",
                source = "chat_compact",
                status = ReceiptStatus.DONE,
                nodeHashes = listOf("h1", "h2"),
                linkCount = 3,
                error = null,
                createdAt = 1_700_000_000L,
                stats = mapOf("turns" to 5),
            )
        val rebuilt = Receipt.fromStoreDict(original.toStoreDict())
        assertEquals("b1", rebuilt.batchId)
        assertEquals("chat_compact", rebuilt.source)
        assertEquals(ReceiptStatus.DONE, rebuilt.status)
        assertEquals(listOf("h1", "h2"), rebuilt.nodeHashes)
        assertEquals(3, rebuilt.linkCount)
        assertNull(rebuilt.error)
        assertEquals(1_700_000_000L, rebuilt.createdAt)
        assertTrue(
            "stats must be empty after rebuild (never persisted)",
            rebuilt.stats.isEmpty(),
        )
    }

    @Test
    fun `Receipt fromStoreDict round-trips a failed receipt carrying error`() {
        val original =
            Receipt(
                batchId = "b2",
                source = "manual",
                status = ReceiptStatus.FAILED,
                nodeHashes = emptyList(),
                linkCount = 0,
                error = "validation blew up",
                createdAt = 42L,
                stats = mapOf("retries" to 2),
            )
        val rebuilt = Receipt.fromStoreDict(original.toStoreDict())
        assertEquals("b2", rebuilt.batchId)
        assertEquals("manual", rebuilt.source)
        assertEquals(ReceiptStatus.FAILED, rebuilt.status)
        assertTrue(rebuilt.nodeHashes.isEmpty())
        assertEquals(0, rebuilt.linkCount)
        assertEquals("validation blew up", rebuilt.error)
        assertEquals(42L, rebuilt.createdAt)
        assertTrue(rebuilt.stats.isEmpty())
    }

    @Test
    fun `Receipt fromStoreDict drops stats even when the source carried them`() {
        val dict =
            mapOf<String, Any?>(
                "batch_id" to "b3",
                "source" to "backfill",
                "status" to "done",
                "node_hashes" to listOf("x"),
                "link_count" to 1,
                "error" to null,
                "created_at" to 99L,
                "stats" to mapOf("turns" to 7, "usage" to mapOf("total" to 4242)),
            )
        val rebuilt = Receipt.fromStoreDict(dict)
        assertNotNull(rebuilt)
        assertTrue(
            "fromStoreDict must ignore any stats key in the source dict",
            rebuilt.stats.isEmpty(),
        )
    }

    @Test
    fun `Receipt fromStoreDict coerces node_hashes entries to strings`() {
        val dict =
            mapOf<String, Any?>(
                "batch_id" to "b",
                "source" to "manual",
                "status" to "done",
                "node_hashes" to listOf(123L, 456, null, "abc"),
                "link_count" to 0,
                "error" to null,
                "created_at" to 0L,
            )
        val rebuilt = Receipt.fromStoreDict(dict)
        assertEquals(
            "Every entry must be stringified; nulls become empty strings",
            listOf("123", "456", "", "abc"),
            rebuilt.nodeHashes,
        )
    }

    @Test
    fun `Receipt fromStoreDict tolerates missing optional fields`() {
        val minimal =
            mapOf<String, Any?>(
                "batch_id" to "b",
                "source" to "manual",
                "status" to "done",
            )
        val rebuilt = Receipt.fromStoreDict(minimal)
        assertEquals("b", rebuilt.batchId)
        assertEquals("manual", rebuilt.source)
        assertEquals(ReceiptStatus.DONE, rebuilt.status)
        assertTrue(rebuilt.nodeHashes.isEmpty())
        assertEquals(0, rebuilt.linkCount)
        assertNull(rebuilt.error)
        assertEquals(0L, rebuilt.createdAt)
        assertTrue(rebuilt.stats.isEmpty())
    }

    @Test
    fun `Receipt fromStoreDict maps an unknown status wire value to FAILED`() {
        val dict =
            mapOf<String, Any?>(
                "batch_id" to "b",
                "source" to "manual",
                "status" to "pending",
            )
        val rebuilt = Receipt.fromStoreDict(dict)
        assertEquals(
            "Unknown status must fall back to FAILED",
            ReceiptStatus.FAILED,
            rebuilt.status,
        )
    }

    @Test
    fun `Receipt defaults match Python defaults`() {
        val receipt = Receipt("b", "manual", ReceiptStatus.DONE)
        assertTrue(receipt.nodeHashes.isEmpty())
        assertEquals(0, receipt.linkCount)
        assertNull(receipt.error)
        assertEquals(0L, receipt.createdAt)
        assertTrue(receipt.stats.isEmpty())
    }
}
