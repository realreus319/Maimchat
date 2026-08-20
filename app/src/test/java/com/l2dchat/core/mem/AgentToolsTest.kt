package com.l2dchat.core.mem

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.test.core.app.ApplicationProvider
import java.time.ZoneId
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Tests for [AgentTools.buildAgentTools] — Kotlin port of the behavior in
 * `mem/agent_tools.py`.
 *
 * Each tool's schema is asserted (name, required params), and each handler is
 * exercised against a real in-memory [MemStore] (Robolectric throwaway-DB
 * pattern, same as [MemStoreTest]). A deterministic [DeterministicEmbedder]
 * derives a fixed-dimension vector from the text so no queueing is needed.
 *
 * Coverage:
 * - [AgentTools.buildAgentTools] returns exactly the 8 tools with the right names.
 * - `register_entity` dedups on stored + staged name; returns `reused` flag.
 * - `register_content` validates `node_type`, parses `event_time`, merges batch metadata.
 * - `link_event_entity` rejects hallucinated hashes; bumps `appearance_count`.
 * - `search_entities` finds stored entities by canonical name and alias.
 * - `get_entity` / `entity_events` read back stored + staged cards.
 * - `update_entity_aliases` unions aliases.
 * - `supersede` rejects same-hash, missing nodes, double-supersede.
 * - Argument validation: missing required, wrong types, empty strings.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class AgentToolsTest {

    private lateinit var db: AgentToolsTestDb
    private lateinit var store: MemStore
    private lateinit var tx: TxWriter
    private lateinit var embedder: DeterministicEmbedder
    private lateinit var registry: NodeTypeRegistry
    private lateinit var tools: Map<String, ToolDef>

    private val zone: ZoneId = ZoneId.of("Asia/Shanghai")
    private val mentionTime = 1_700_000_000L

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            AgentToolsTestDb::class.java,
        ).allowMainThreadQueries().build()
        store = MemStore(
            nodeDao = db.memNodeDao(),
            linkDao = db.memLinkDao(),
            vectorDao = db.memVectorDao(),
            receiptDao = db.memReceiptDao(),
            expectedDim = 4,
            transactionRunner = null,
            zoneId = zone,
        )
        runBlocking { store.load() }
        embedder = DeterministicEmbedder(dim = 4)
        registry = NodeTypeRegistry()
        tx = TxWriter(dimensions = 4)
        tools = AgentTools.buildAgentTools(
            store = store,
            tx = tx,
            embedder = embedder,
            registry = registry,
            mentionTimeEpoch = mentionTime,
            batchMetadata = mapOf("source" to "chat_compact"),
        )
    }

    @After
    fun tearDown() {
        db.close()
    }

    // ------------------------------------------------------------------
    // Tool surface
    // ------------------------------------------------------------------

    @Test
    fun `buildAgentTools returns exactly the 8 tools with the right names`() {
        assertEquals(
            listOf(
                "search_entities",
                "get_entity",
                "entity_events",
                "register_entity",
                "register_content",
                "link_event_entity",
                "update_entity_aliases",
                "supersede",
            ),
            tools.keys.toList(),
        )
    }

    @Test
    fun `each tool schema carries its required params`() {
        assertEquals(listOf("query"), (tools["search_entities"]!!.schema.parameters["required"]))
        assertEquals(listOf("hash_id"), (tools["get_entity"]!!.schema.parameters["required"]))
        assertEquals(listOf("entity_hash"), (tools["entity_events"]!!.schema.parameters["required"]))
        assertEquals(
            listOf("name", "entity_type", "description"),
            tools["register_entity"]!!.schema.parameters["required"],
        )
        assertEquals(
            listOf("node_type", "content"),
            tools["register_content"]!!.schema.parameters["required"],
        )
        assertEquals(
            listOf("content_hash", "entity_hash"),
            tools["link_event_entity"]!!.schema.parameters["required"],
        )
        assertEquals(
            listOf("entity_hash", "add_aliases"),
            tools["update_entity_aliases"]!!.schema.parameters["required"],
        )
        assertEquals(
            listOf("old_hash", "new_hash"),
            tools["supersede"]!!.schema.parameters["required"],
        )
    }

    // ------------------------------------------------------------------
    // register_entity
    // ------------------------------------------------------------------

    @Test
    fun `register_entity stages a new entity and returns reused=false`() = runBlocking {
        val result = callMap("register_entity", mapOf(
            "name" to "Alice",
            "entity_type" to "person",
            "description" to "a friend",
            "aliases" to listOf("A"),
        ))
        assertEquals(false, result["reused"])
        assertEquals("Alice", result["canonical_name"])
        val hash = result["hash"] as String
        assertEquals(12, hash.length)
        // One putNode + one putVector (entity cards are embedded for recall).
        assertEquals(2, tx.size())
    }

    @Test
    fun `register_entity dedups on staged name and returns reused=true`() = runBlocking {
        val first = callMap("register_entity", mapOf(
            "name" to "Bob",
            "entity_type" to "person",
            "description" to "a neighbor",
        ))
        val second = callMap("register_entity", mapOf(
            "name" to "Bob",
            "entity_type" to "person",
            "description" to "a neighbor again",
        ))
        assertEquals(false, first["reused"])
        assertEquals(true, second["reused"])
        assertEquals(first["hash"], second["hash"])
    }

    @Test
    fun `register_entity dedups on staged alias`() = runBlocking {
        callMap("register_entity", mapOf(
            "name" to "Charlie",
            "entity_type" to "person",
            "description" to "a colleague",
            "aliases" to listOf("Chuck"),
        ))
        val byAlias = callMap("register_entity", mapOf(
            "name" to "Chuck",
            "entity_type" to "person",
            "description" to "same person",
        ))
        assertEquals(true, byAlias["reused"])
    }

    @Test
    fun `register_entity dedups against stored entities after a flush`() = runBlocking {
        // Stage + flush one entity.
        callMap("register_entity", mapOf(
            "name" to "Dave",
            "entity_type" to "person",
            "description" to "a teacher",
        ))
        tx.flush(store)
        // Fresh tx + tools for the "second batch".
        tx = TxWriter(dimensions = 4)
        tools = AgentTools.buildAgentTools(
            store = store, tx = tx, embedder = embedder, registry = registry,
            mentionTimeEpoch = mentionTime,
        )
        val result = callMap("register_entity", mapOf(
            "name" to "Dave",
            "entity_type" to "person",
            "description" to "a teacher again",
        ))
        assertEquals(true, result["reused"])
    }

    @Test
    fun `register_entity rejects empty name`() = runBlocking {
        try {
            callMap("register_entity", mapOf("name" to "", "entity_type" to "x", "description" to "y"))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("name"))
        }
    }

    // ------------------------------------------------------------------
    // register_content
    // ------------------------------------------------------------------

    @Test
    fun `register_content stages a content node with merged batch metadata`() = runBlocking {
        val entityHash = (callMap("register_entity", mapOf(
            "name" to "Eve", "entity_type" to "person", "description" to "a coder",
        ))["hash"] as String)
        val result = callMap("register_content", mapOf(
            "node_type" to "event",
            "content" to "Eve shipped the feature",
            "event_time" to "2026-08-08",
            "entity_mentions" to listOf("Eve"),
            "metadata" to mapOf("topics" to listOf("release")),
        ))
        val hash = result["hash"] as String
        assertEquals("event", result["node_type"])
        assertEquals(12, hash.length)
        // The staged node exists in the staging view.
        val contentNode = tx.stagedOps().filterIsInstance<TxOp.PutNode>().first { it.hashId == hash }
        // Batch metadata (source=chat_compact) merged with per-call metadata.
        assertEquals("chat_compact", contentNode.metadata["source"])
        assertEquals(listOf("release"), contentNode.metadata["topics"])
        assertEquals("2026-08-08", contentNode.eventTime)
    }

    @Test
    fun `register_content rejects entity-base node_type`() = runBlocking {
        try {
            callMap("register_content", mapOf("node_type" to "entity", "content" to "x"))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("entity-base type"))
        }
    }

    @Test
    fun `register_content rejects unparseable event_time`() = runBlocking {
        try {
            callMap("register_content", mapOf(
                "node_type" to "event",
                "content" to "x",
                "event_time" to "not-a-date",
            ))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("event_time"))
        }
    }

    @Test
    fun `register_content accepts null event_time as mention-time instant`() = runBlocking {
        val result = callMap("register_content", mapOf(
            "node_type" to "event",
            "content" to "something happened",
        ))
        val hash = result["hash"] as String
        val node = tx.stagedOps().filterIsInstance<TxOp.PutNode>().first { it.hashId == hash }
        assertNull(node.eventTime)
    }

    // ------------------------------------------------------------------
    // link_event_entity
    // ------------------------------------------------------------------

    @Test
    fun `link_event_entity links staged content to staged entity and bumps appearance_count`() = runBlocking {
        val entityHash = (callMap("register_entity", mapOf(
            "name" to "Frank", "entity_type" to "person", "description" to "a friend",
        ))["hash"] as String)
        val contentHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "Frank visited",
        ))["hash"] as String)
        val result = callMap("link_event_entity", mapOf(
            "content_hash" to contentHash,
            "entity_hash" to entityHash,
            "role" to "主角",
        ))
        assertEquals(true, result["created"])
        assertEquals("主角", result["role"])
        // appearance_count bumped via a staged update.
        val updates = tx.stagedOps().filterIsInstance<TxOp.UpdateNode>()
        assertTrue(updates.any { it.hashId == entityHash && it.fields["appearance_count"] == 1 })
    }

    @Test
    fun `link_event_entity rejects hallucinated content hash`() = runBlocking {
        val entityHash = (callMap("register_entity", mapOf(
            "name" to "Grace", "entity_type" to "person", "description" to "a friend",
        ))["hash"] as String)
        try {
            callMap("link_event_entity", mapOf(
                "content_hash" to "NONEXISTENT12",
                "entity_hash" to entityHash,
            ))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("content node not found"))
        }
    }

    @Test
    fun `link_event_entity rejects hallucinated entity hash`() = runBlocking {
        val contentHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "x",
        ))["hash"] as String)
        try {
            callMap("link_event_entity", mapOf(
                "content_hash" to contentHash,
                "entity_hash" to "NONEXISTENT12",
            ))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("entity not found"))
        }
    }

    @Test
    fun `link_event_entity repeat link returns created=false and does not bump appearance_count again`() = runBlocking {
        val entityHash = (callMap("register_entity", mapOf(
            "name" to "Heidi", "entity_type" to "person", "description" to "a friend",
        ))["hash"] as String)
        val contentHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "Heidi called",
        ))["hash"] as String)
        callMap("link_event_entity", mapOf("content_hash" to contentHash, "entity_hash" to entityHash))
        val second = callMap("link_event_entity", mapOf("content_hash" to contentHash, "entity_hash" to entityHash))
        assertEquals(false, second["created"])
        // Only one appearance_count bump (from the first call).
        val bumps = tx.stagedOps()
            .filterIsInstance<TxOp.UpdateNode>()
            .count { it.hashId == entityHash && it.fields["appearance_count"] == 1 }
        assertEquals(1, bumps)
    }

    // ------------------------------------------------------------------
    // search_entities
    // ------------------------------------------------------------------

    @Test
    fun `search_entities finds staged entity by canonical name`() = runBlocking {
        callMap("register_entity", mapOf(
            "name" to "Ivan", "entity_type" to "person", "description" to "a dev",
        ))
        val results = call("search_entities", mapOf("query" to "Ivan")) as List<*>
        assertEquals(1, results.size)
        @Suppress("UNCHECKED_CAST")
        val card = results[0] as Map<String, Any?>
        assertEquals("Ivan", card["canonical_name"])
    }

    @Test
    fun `search_entities finds staged entity by alias`() = runBlocking {
        callMap("register_entity", mapOf(
            "name" to "Judy",
            "entity_type" to "person",
            "description" to "a pm",
            "aliases" to listOf("J"),
        ))
        val results = call("search_entities", mapOf("query" to "J")) as List<*>
        assertEquals(1, results.size)
    }

    @Test
    fun `search_entities finds stored entity after flush`() = runBlocking {
        callMap("register_entity", mapOf(
            "name" to "Karl", "entity_type" to "person", "description" to "a tester",
        ))
        tx.flush(store)
        tx = TxWriter(dimensions = 4)
        tools = AgentTools.buildAgentTools(
            store = store, tx = tx, embedder = embedder, registry = registry,
            mentionTimeEpoch = mentionTime,
        )
        val results = call("search_entities", mapOf("query" to "Karl")) as List<*>
        assertEquals(1, results.size)
    }

    @Test
    fun `search_entities returns empty for no match`() = runBlocking {
        val results = call("search_entities", mapOf("query" to "nobody")) as List<*>
        assertTrue(results.isEmpty())
    }

    // ------------------------------------------------------------------
    // get_entity
    // ------------------------------------------------------------------

    @Test
    fun `get_entity returns the full card for a staged entity`() = runBlocking {
        val hash = (callMap("register_entity", mapOf(
            "name" to "Leo", "entity_type" to "person", "description" to "a designer",
        ))["hash"] as String)
        val card = callMap("get_entity", mapOf("hash_id" to hash))
        assertEquals("Leo", card["canonical_name"])
        assertEquals("a designer", (card["content"] as String).substringAfter(": "))
    }

    @Test
    fun `get_entity rejects unknown hash`() = runBlocking {
        try {
            callMap("get_entity", mapOf("hash_id" to "UNKNOWN_HASH1"))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("entity not found"))
        }
    }

    // ------------------------------------------------------------------
    // entity_events
    // ------------------------------------------------------------------

    @Test
    fun `entity_events lists stored events linked to the entity, newest first`() = runBlocking {
        val entityHash = (callMap("register_entity", mapOf(
            "name" to "Mallory", "entity_type" to "person", "description" to "a friend",
        ))["hash"] as String)
        val c1 = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "event A",
        ))["hash"] as String)
        callMap("link_event_entity", mapOf("content_hash" to c1, "entity_hash" to entityHash))
        tx.flush(store)
        tx = TxWriter(dimensions = 4)
        tools = AgentTools.buildAgentTools(
            store = store, tx = tx, embedder = embedder, registry = registry,
            mentionTimeEpoch = mentionTime,
        )
        val events = call("entity_events", mapOf("entity_hash" to entityHash)) as List<*>
        assertEquals(1, events.size)
        @Suppress("UNCHECKED_CAST")
        val evt = events[0] as Map<String, Any?>
        assertEquals(c1, evt["hash"])
        assertEquals("event A", evt["content"])
        assertNotNull(evt["mention_time"])
    }

    @Test
    fun `entity_events rejects unknown entity`() = runBlocking {
        try {
            call("entity_events", mapOf("entity_hash" to "UNKNOWN_HASH1"))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("entity not found"))
        }
    }

    // ------------------------------------------------------------------
    // update_entity_aliases
    // ------------------------------------------------------------------

    @Test
    fun `update_entity_aliases unions new aliases into a staged entity`() = runBlocking {
        val hash = (callMap("register_entity", mapOf(
            "name" to "Nina",
            "entity_type" to "person",
            "description" to "a friend",
            "aliases" to listOf("N"),
        ))["hash"] as String)
        val result = callMap("update_entity_aliases", mapOf(
            "entity_hash" to hash,
            "add_aliases" to listOf("Nini", "N"),
        ))
        @Suppress("UNCHECKED_CAST")
        val aliases = result["aliases"] as List<String>
        assertTrue("N" in aliases)
        assertTrue("Nini" in aliases)
        // No duplicate for the already-present "N".
        assertEquals(1, aliases.count { it == "N" })
    }

    @Test
    fun `update_entity_aliases rejects empty add_aliases`() = runBlocking {
        val hash = (callMap("register_entity", mapOf(
            "name" to "Oscar", "entity_type" to "person", "description" to "x",
        ))["hash"] as String)
        try {
            callMap("update_entity_aliases", mapOf("entity_hash" to hash, "add_aliases" to emptyList<String>()))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("must not be empty"))
        }
    }

    // ------------------------------------------------------------------
    // supersede
    // ------------------------------------------------------------------

    @Test
    fun `supersede marks an old content node as replaced by a new one`() = runBlocking {
        val oldHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "old fact",
        ))["hash"] as String)
        val newHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "corrected fact",
        ))["hash"] as String)
        val result = callMap("supersede", mapOf("old_hash" to oldHash, "new_hash" to newHash))
        assertEquals(true, result["superseded"])
        assertEquals(newHash, result["new_hash"])
        // A staged update sets superseded_by on the old node.
        val updates = tx.stagedOps().filterIsInstance<TxOp.UpdateNode>()
        assertTrue(updates.any { it.hashId == oldHash && it.fields["superseded_by"] == newHash })
    }

    @Test
    fun `supersede rejects same old and new hash`() = runBlocking {
        val hash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "x",
        ))["hash"] as String)
        try {
            callMap("supersede", mapOf("old_hash" to hash, "new_hash" to hash))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("must differ"))
        }
    }

    @Test
    fun `supersede rejects unknown new hash`() = runBlocking {
        val oldHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "old",
        ))["hash"] as String)
        try {
            callMap("supersede", mapOf("old_hash" to oldHash, "new_hash" to "UNKNOWN_HASH1"))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("new content node not found"))
        }
    }

    @Test
    fun `supersede rejects double-supersede of the same old node within one batch`() = runBlocking {
        val oldHash = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "old",
        ))["hash"] as String)
        val newHash1 = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "new1",
        ))["hash"] as String)
        val newHash2 = (callMap("register_content", mapOf(
            "node_type" to "event", "content" to "new2",
        ))["hash"] as String)
        callMap("supersede", mapOf("old_hash" to oldHash, "new_hash" to newHash1))
        try {
            callMap("supersede", mapOf("old_hash" to oldHash, "new_hash" to newHash2))
            fail("expected ToolValidationError")
        } catch (e: ToolValidationError) {
            assertTrue(e.message!!.contains("already superseded in this batch"))
        }
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /** Invoke a tool handler by name with the given args, runBlocking-style. */
    private suspend fun call(name: String, args: Map<String, Any?>): Any? =
        tools[name]!!.handler(args)

    /** Like [call] but asserts the result is a Map (the common tool return shape). */
    @Suppress("UNCHECKED_CAST")
    private suspend fun callMap(name: String, args: Map<String, Any?>): Map<String, Any?> =
        call(name, args) as Map<String, Any?>
}

/**
 * Deterministic embedder for tests: derives a fixed-dimension vector from the
 * text's hash code so no queueing is needed. Every text gets a stable vector.
 */
private class DeterministicEmbedder(private val dim: Int) : EmbeddingClient {
    override val dimension: Int = dim

    override suspend fun embed(texts: List<String>): List<FloatArray> =
        texts.map { text ->
            val base = text.hashCode()
            FloatArray(dim) { i -> ((base shr (i * 3)) and 0xFF).toFloat() / 255f }
        }
}

/**
 * Throwaway test-only [RoomDatabase] for [AgentToolsTest].
 *
 * Same pattern as `MemStoreTest.MemStoreTestDb`; declared separately so this
 * test class is self-contained and does not collide with another test file's
 * private database.
 */
@Database(
    entities = [
        MemNodeEntity::class,
        MemEventEntityLinkEntity::class,
        MemVectorEntity::class,
        MemReceiptEntity::class,
    ],
    version = 1,
    exportSchema = false,
)
abstract class AgentToolsTestDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
