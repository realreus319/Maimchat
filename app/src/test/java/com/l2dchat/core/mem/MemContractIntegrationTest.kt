package com.l2dchat.core.mem

import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Phase 0 closing gate (plan task T0.7) — contract-layer integration test.
 *
 * Exercises the six contract modules together, exactly the way the extraction
 * pipeline will use them: an [IngestBatch] comes in, a content-addressed
 * [Node] hash is derived via [Hashing.shortHash], the [Node] is built with the
 * declarative `event` type, the [NodeTypeRegistry] confirms that type is part
 * of the extraction prompt, and [TimeParse.parseEventTime] expands the
 * normalized `event_time` into the `[startTs, endTs)` interval that gets filled
 * into the node.
 *
 * Epoch-second expectations are cross-validated against the Python reference
 * (`mem/timeparse.py::parse_event_time`) under `TZ=Asia/Shanghai`; see
 * [TimeParseTest] for the per-case provenance.
 */
class MemContractIntegrationTest {

    private val zone = ZoneId.of("Asia/Shanghai")

    @Test
    fun `IngestBatch to Node pipeline wires source, hash, registry and time parse together`() {
        // 1. Host assembles a chat_compact batch (design section 0 input contract).
        val mentionTime = 1_786_118_400L
        val batch =
            IngestBatch(
                batchId = "compact_ctx1_42",
                source = BatchSource.CHAT_COMPACT,
                text = "[15:30 user] 用户提到了项目X",
                timeAnchor = mentionTime,
                mentionTime = mentionTime,
            )
        assertEquals(BatchSource.CHAT_COMPACT, batch.source)
        assertEquals("chat_compact", batch.source.wireValue)

        // 2. Extraction decides this is an event; content + normalized event_time
        //    become the content-addressed key (mirrors `short_hash` in node.py).
        val content = "用户提到了项目X"
        val eventTime = "2026-08-08"
        val hashId = Hashing.shortHash("event$content$eventTime")
        assertEquals(12, hashId.length)
        // Same input must hash deterministically — content addressing contract.
        assertEquals(hashId, Hashing.shortHash("event$content$eventTime"))

        // 3. Registry confirms `event` is a known content-base type and emits the
        //    extraction-prompt fragment the model will see.
        val registry = NodeTypeRegistry()
        assertTrue("event must be a registered type", registry.has("event"))
        val eventSpec = registry.get("event")
        assertEquals("content", eventSpec.baseType)
        val promptSpec = registry.promptSpec()
        assertTrue(
            "promptSpec must contain the event section header",
            "## event (base_type: content)" in promptSpec,
        )
        assertTrue(
            "promptSpec must contain the event prompt fragment",
            eventSpec.promptFragment in promptSpec,
        )
        assertTrue(
            "promptSpec must document the participants metadata field",
            "  - participants: list[str]: names of the people involved" in promptSpec,
        )

        // 4. Parse the normalized event_time into a half-open epoch-second interval.
        //    Python: parse_event_time("2026-08-08") == (1786118400.0, 1786204800.0, False)
        val (startTs, endTs, approx) = TimeParse.parseEventTime(eventTime, zone)
        assertEquals(1786118400L, startTs)
        assertEquals(1786204800L, endTs)
        assertFalse("day-precision point is not approximate", approx)
        assertTrue(
            "interval must be half-open and span exactly one day",
            endTs - startTs == 24L * 60 * 60,
        )

        // 5. Build the Node, filling the derived interval from step 4.
        val node =
            Node(
                hashId = hashId,
                baseType = "content",
                nodeType = "event",
                content = content,
                mentionTime = batch.mentionTime,
                eventTimeRaw = "下周六",
                eventTime = eventTime,
                startTs = startTs,
                endTs = endTs,
                importance = 0.8,
                sentiment = -0.2,
                metadata =
                    mapOf(
                        "participants" to listOf("user"),
                        "topics" to listOf("项目X"),
                    ),
            )
        assertEquals(hashId, node.hashId)
        assertEquals("content", node.baseType)
        assertEquals("event", node.nodeType)
        assertEquals(content, node.content)
        assertEquals(batch.mentionTime, node.mentionTime)
        assertEquals(startTs, node.startTs)
        assertEquals(endTs, node.endTs)
        assertEquals("下周六", node.eventTimeRaw)
        assertEquals(eventTime, node.eventTime)

        // 6. The node must round-trip through the row shape used by the storage
        //    layer (W1/R1 atoms), preserving every field including the interval.
        val row = node.toRow()
        assertEquals(hashId, row["hash_id"])
        assertEquals("content", row["base_type"])
        assertEquals("event", row["node_type"])
        assertEquals(startTs, row["start_ts"])
        assertEquals(endTs, row["end_ts"])
        assertEquals(eventTime, row["event_time"])
        assertEquals("下周六", row["event_time_raw"])
        val reconstructed = Node.fromRow(row)
        assertEquals(node, reconstructed)
    }

    @Test
    fun `Receipt records the hashes written for a chat_compact batch`() {
        // The pipeline returns a Receipt whose node_hashes are the content
        // addresses produced above; rebuilding it from the store dict must keep
        // those hashes and drop the non-persisted stats.
        val hashId = Hashing.shortHash("event用户提到了项目X2026-08-08")
        val receipt =
            Receipt(
                batchId = "compact_ctx1_42",
                source = BatchSource.CHAT_COMPACT.wireValue,
                status = ReceiptStatus.DONE,
                nodeHashes = listOf(hashId),
                linkCount = 0,
                createdAt = 1_786_118_400L,
                stats = mapOf("turns" to 1),
            )
        val rebuilt = Receipt.fromStoreDict(receipt.toStoreDict())
        assertEquals(listOf(hashId), rebuilt.nodeHashes)
        assertEquals(ReceiptStatus.DONE, rebuilt.status)
        assertTrue(
            "stats must not survive the store round-trip",
            rebuilt.stats.isEmpty(),
        )
    }

    @Test
    fun `open-ended event_time interval is representable on a Node`() {
        // `A/..` yields endTs = Long.MAX_VALUE; the Node must carry it verbatim
        // so retrieval's interval-overlap query keeps matching ongoing events.
        val (startTs, endTs, approx) = TimeParse.parseEventTime("2025-12/..", zone)
        assertEquals(Long.MAX_VALUE, endTs)
        assertFalse(approx)
        val node =
            Node(
                hashId = Hashing.shortHash("eventongoing2025-12/.."),
                baseType = "content",
                nodeType = "event",
                content = "ongoing thing",
                mentionTime = 1_764_518_400L,
                eventTime = "2025-12/..",
                startTs = startTs,
                endTs = endTs,
            )
        assertEquals(startTs, node.startTs)
        assertEquals(Long.MAX_VALUE, node.endTs)
        // Round-trip must preserve the sentinel exactly.
        val rebuilt = Node.fromRow(node.toRow())
        assertEquals(Long.MAX_VALUE, rebuilt.endTs)
    }

    @Test
    fun `every builtin node type is useable as a Node base_type slash node_type pair`() {
        // Guards against drift between BASE_TYPES, the Node init check, and the
        // builtin registry specs — all three must agree for every builtin type.
        val registry = NodeTypeRegistry()
        for (spec in NodeTypeRegistry.BUILTIN_SPECS) {
            assertTrue(
                "builtin base_type '${spec.baseType}' must be in BASE_TYPES",
                spec.baseType in BASE_TYPES,
            )
            val node =
                Node(
                    hashId = Hashing.shortHash("builtin${spec.nodeType}"),
                    baseType = spec.baseType,
                    nodeType = spec.nodeType,
                    content = "probe for ${spec.nodeType}",
                    mentionTime = 0L,
                )
            assertEquals(spec.nodeType, node.nodeType)
            assertEquals(spec.baseType, node.baseType)
            assertNotNull(registry.get(spec.nodeType))
        }
    }
}
