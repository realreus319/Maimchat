package com.l2dchat.core.mem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Kotlin port of the Python `Node` dataclass contract — see `mem/node.py`.
 *
 * Covers init validation, the `to_row`/`from_row` round-trip, deterministic
 * sorted-key metadata JSON, and default values.
 */
class NodeTest {
    private fun sampleNode(
        hashId: String = "abc123def456",
        baseType: String = "content",
        metadata: Map<String, Any?> = emptyMap(),
    ): Node =
        Node(
            hashId = hashId,
            baseType = baseType,
            nodeType = "event",
            content = "用户提到了项目X",
            mentionTime = 1723084800L,
            eventTimeRaw = "下周六",
            eventTime = "2026-08-08",
            startTs = 1783526400L,
            endTs = 1783612800L,
            importance = 0.8,
            sentiment = -0.2,
            metadata = metadata,
            supersededBy = null,
            accessCount = 3,
            lastAccessed = 1723100000L,
            createdAt = 1723084801L,
            updatedAt = 1723100001L,
        )

    @Test
    fun `BASE_TYPES contains exactly content and entity`() {
        assertEquals(setOf("content", "entity"), BASE_TYPES)
    }

    @Test
    fun `Node accepts content base_type`() {
        val node = sampleNode(baseType = "content")
        assertEquals("content", node.baseType)
    }

    @Test
    fun `Node accepts entity base_type`() {
        val node = sampleNode(baseType = "entity")
        assertEquals("entity", node.baseType)
    }

    @Test
    fun `Node rejects empty hash_id with IllegalArgumentException`() {
        try {
            sampleNode(hashId = "")
            fail("Expected IllegalArgumentException for empty hash_id")
        } catch (e: IllegalArgumentException) {
            assertTrue(
                "Message must mention hash_id: ${e.message}",
                e.message?.contains("hash_id") == true,
            )
        }
    }

    @Test
    fun `Node rejects bad base_type with IllegalArgumentException`() {
        try {
            sampleNode(baseType = "memory")
            fail("Expected IllegalArgumentException for bad base_type")
        } catch (e: IllegalArgumentException) {
            assertTrue(
                "Message must mention base_type: ${e.message}",
                e.message?.contains("base_type") == true,
            )
            assertTrue(
                "Message must echo the bad value: ${e.message}",
                e.message?.contains("'memory'") == true,
            )
        }
    }

    @Test
    fun `Node defaults match the Python dataclass defaults`() {
        val node =
            Node(
                hashId = "h00000000001",
                baseType = "content",
                nodeType = "event",
                content = "hello",
                mentionTime = 1L,
            )
        assertNull(node.eventTimeRaw)
        assertNull(node.eventTime)
        assertNull(node.startTs)
        assertNull(node.endTs)
        assertNull(node.importance)
        assertNull(node.sentiment)
        assertTrue("metadata must default to empty", node.metadata.isEmpty())
        assertNull(node.supersededBy)
        assertEquals(0, node.accessCount)
        assertNull(node.lastAccessed)
        assertEquals(0L, node.createdAt)
        assertEquals(0L, node.updatedAt)
    }

    @Test
    fun `toRow produces the nodes table column shape`() {
        val node = sampleNode()
        val row = node.toRow()
        val expectedKeys =
            setOf(
                "hash_id",
                "base_type",
                "node_type",
                "content",
                "mention_time",
                "event_time_raw",
                "event_time",
                "start_ts",
                "end_ts",
                "importance",
                "sentiment",
                "metadata_json",
                "superseded_by",
                "access_count",
                "last_accessed",
                "created_at",
                "updated_at",
            )
        assertEquals(expectedKeys, row.keys)
        assertEquals("abc123def456", row["hash_id"])
        assertEquals("content", row["base_type"])
        assertEquals("event", row["node_type"])
        assertEquals(1723084800L, row["mention_time"])
        assertEquals(1783526400L, row["start_ts"])
        assertEquals(1783612800L, row["end_ts"])
        assertEquals(0.8, row["importance"])
        assertEquals(-0.2, row["sentiment"])
        assertEquals(3, row["access_count"])
        assertEquals(1723084801L, row["created_at"])
        assertEquals(1723100001L, row["updated_at"])
        assertEquals("{}", row["metadata_json"])
    }

    @Test
    fun `fromRow reconstructs a node with all fields preserved`() {
        val original = sampleNode(metadata = mapOf("participants" to listOf("alice", "bob")))
        val reconstructed = Node.fromRow(original.toRow())
        assertEquals(original, reconstructed)
    }

    @Test
    fun `fromRow round-trip preserves nullable fields set to null`() {
        val original =
            Node(
                hashId = "h00000000002",
                baseType = "entity",
                nodeType = "entity",
                content = "alice: engineer",
                mentionTime = 5L,
            )
        val reconstructed = Node.fromRow(original.toRow())
        assertEquals(original, reconstructed)
        assertNull(reconstructed.eventTimeRaw)
        assertNull(reconstructed.startTs)
        assertNull(reconstructed.importance)
        assertNull(reconstructed.supersededBy)
        assertNull(reconstructed.lastAccessed)
    }

    @Test
    fun `fromRow tolerates null access_count and timestamps like the Python fallback`() {
        val row =
            mapOf<String, Any?>(
                "hash_id" to "h00000000003",
                "base_type" to "content",
                "node_type" to "event",
                "content" to "x",
                "mention_time" to 7L,
                "event_time_raw" to null,
                "event_time" to null,
                "start_ts" to null,
                "end_ts" to null,
                "importance" to null,
                "sentiment" to null,
                "metadata_json" to null,
                "superseded_by" to null,
                "access_count" to null,
                "last_accessed" to null,
                "created_at" to null,
                "updated_at" to null,
            )
        val node = Node.fromRow(row)
        assertEquals(0, node.accessCount)
        assertEquals(0L, node.createdAt)
        assertEquals(0L, node.updatedAt)
        assertTrue(node.metadata.isEmpty())
    }

    @Test
    fun `metadata JSON serializes keys in sorted order`() {
        val node =
            sampleNode(
                metadata =
                    mapOf(
                        "b" to 1,
                        "a" to 2,
                        "c" to 3,
                    ),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"a":2,"b":1,"c":3}""", json)
    }

    @Test
    fun `metadata JSON is deterministic regardless of insertion order`() {
        val orderedInsertion =
            sampleNode(
                metadata =
                    linkedMapOf(
                        "a" to 1,
                        "b" to 2,
                        "c" to 3,
                    ),
            )
        val reversedInsertion =
            sampleNode(
                metadata =
                    linkedMapOf(
                        "c" to 3,
                        "b" to 2,
                        "a" to 1,
                    ),
            )
        val jsonA = orderedInsertion.toRow()["metadata_json"] as String
        val jsonB = reversedInsertion.toRow()["metadata_json"] as String
        assertEquals(jsonA, jsonB)
        assertEquals("""{"a":1,"b":2,"c":3}""", jsonA)
    }

    @Test
    fun `metadata JSON preserves non-ASCII characters verbatim like ensure_ascii equals False`() {
        val node =
            sampleNode(
                metadata = mapOf("name" to "罗联政府"),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"name":"罗联政府"}""", json)
    }

    @Test
    fun `metadata JSON escapes control characters and quotes`() {
        val node =
            sampleNode(
                metadata = mapOf("quote" to "she said \"hi\"\n"),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"quote":"she said \"hi\"\n"}""", json)
    }

    @Test
    fun `metadata JSON serializes nested objects with sorted keys at every level`() {
        val node =
            sampleNode(
                metadata =
                    mapOf(
                        "outer" to
                            mapOf(
                                "z" to 1,
                                "a" to 2,
                            ),
                    ),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"outer":{"a":2,"z":1}}""", json)
    }

    @Test
    fun `metadata JSON serializes lists in insertion order`() {
        val node =
            sampleNode(
                metadata =
                    mapOf(
                        "participants" to listOf("zoe", "alice", "bob"),
                    ),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"participants":["zoe","alice","bob"]}""", json)
    }

    @Test
    fun `metadata JSON serializes booleans and null`() {
        val node =
            sampleNode(
                metadata =
                    mapOf(
                        "flag" to true,
                        "missing" to null,
                    ),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"flag":true,"missing":null}""", json)
    }

    @Test
    fun `metadata JSON serializes doubles with trailing dot zero`() {
        val node =
            sampleNode(
                metadata = mapOf("score" to 1.0),
            )
        val json = node.toRow()["metadata_json"] as String
        assertEquals("""{"score":1.0}""", json)
    }

    @Test
    fun `metadata JSON round-trips through fromRow`() {
        // Integers are parsed back as Long (JSON has no Int/Long distinction), so
        // the original uses Long values to keep the round-trip structurally equal.
        val original =
            sampleNode(
                metadata =
                    mapOf(
                        "participants" to listOf("alice", "bob"),
                        "topics" to listOf("kotlin", "mem"),
                        "count" to 2L,
                        "score" to 0.5,
                        "flag" to true,
                        "nested" to mapOf("z" to 1L, "a" to 2L),
                    ),
            )
        val reconstructed = Node.fromRow(original.toRow())
        assertEquals(original.metadata, reconstructed.metadata)
    }

    @Test
    fun `metadata JSON round-trip normalizes integers to Long`() {
        val original =
            sampleNode(
                metadata = mapOf("count" to 2),
            )
        val reconstructed = Node.fromRow(original.toRow())
        // JSON carries no Int vs Long distinction; the parser yields Long.
        assertEquals(2L, reconstructed.metadata["count"])
    }

    @Test
    fun `metadata JSON round-trip preserves CJK strings`() {
        val original =
            sampleNode(
                metadata =
                    mapOf(
                        "name" to "罗联政府",
                        "aliases" to listOf("罗联", "政府"),
                    ),
            )
        val reconstructed = Node.fromRow(original.toRow())
        assertEquals(original.metadata, reconstructed.metadata)
    }

    @Test
    fun `fromRow parses blank metadata_json as empty map`() {
        val row =
            mapOf<String, Any?>(
                "hash_id" to "h00000000004",
                "base_type" to "content",
                "node_type" to "event",
                "content" to "x",
                "mention_time" to 1L,
                "event_time_raw" to null,
                "event_time" to null,
                "start_ts" to null,
                "end_ts" to null,
                "importance" to null,
                "sentiment" to null,
                "metadata_json" to "",
                "superseded_by" to null,
                "access_count" to 0,
                "last_accessed" to null,
                "created_at" to 0L,
                "updated_at" to 0L,
            )
        val node = Node.fromRow(row)
        assertTrue(node.metadata.isEmpty())
    }

    @Test
    fun `Node data class equality is structural`() {
        val a = sampleNode()
        val b = sampleNode()
        assertEquals(a, b)
        assertEquals(a.hashCode(), b.hashCode())
    }

    @Test
    fun `Node data class inequality reflects field differences`() {
        val a = sampleNode()
        val b = sampleNode().copy(accessCount = 4)
        assertNotEquals(a, b)
    }
}
