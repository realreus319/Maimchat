package com.l2dchat.core.mem

import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.MemSettings
import com.l2dchat.core.storage.ChatDatabase
import com.l2dchat.core.tools.Tool
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Wiring tests for [MemRuntimeFactory] — the dedicated factory that assembles
 * the mem subsystem and exposes the ltm tool surface as app [Tool]s.
 *
 * Coverage (per phase-6b task spec):
 * - Mem disabled ([MemSettings.enabled] = false) → zero tools, no mem object
 *   constructed, no `store.load()` job launched.
 * - Mem enabled with a resolvable config → exactly the five ltm tools
 *   (`ltm_search`, `ltm_expand`, `ltm_read_media`, `ltm_ask`, `ltm_store`)
 *   registered as [Tool]s, each an [LtmToolAdapter].
 * - `ltm_store` present when the pipeline is built (enableStore defaults to
 *   true — see [MemRuntimeFactory] doc).
 * - `ltm_ask` present whenever mem is enabled (the search sub agent is
 *   constructed alongside the extraction pipeline — see [MemRuntimeFactory]).
 * - `store.load()` job is launched and completes against a real SQLite engine.
 *
 * Uses the real [ChatDatabase] via [Room.inMemoryDatabaseBuilder] (same pattern
 * as [ChatDatabaseMigrationTest]) so the factory's `database.withTransaction`
 * wiring is exercised against the actual Room database, not a stub.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class MemRuntimeFactoryTest {

    private lateinit var db: ChatDatabase
    private lateinit var scope: CoroutineScope

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            ChatDatabase::class.java,
        ).allowMainThreadQueries().build()
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
    }

    @After
    fun tearDown() {
        db.close()
        scope.cancel()
    }

    @Test
    fun `disabled mem settings produce zero tools and no load job`() {
        val settings = MemSettings(enabled = false)
        val localLlm = localLlmSettings()

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        assertTrue("disabled mem must register zero tools", runtime.tools.isEmpty())
        assertEquals("disabled mem must not launch store.load()", null, runtime.lastLoadJob)
        assertNull("disabled mem must expose no pipeline", runtime.pipeline)
        assertNull("disabled mem must expose no store", runtime.store)
        assertNull("disabled mem must expose no retrieval", runtime.retrieval)
    }

    @Test
    fun `enabled mem with resolvable config registers all five ltm tools`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extraction.example.com/v1",
            extractionApiKey = "key-extraction",
            embeddingBaseUrl = "https://embedding.example.com/v1",
            embeddingApiKey = "key-embedding",
            embeddingDimensions = 8,
        )
        val localLlm = localLlmSettings()

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        val names = runtime.tools.map { it.definition.name }.sorted()
        assertEquals(
            listOf("ltm_ask", "ltm_expand", "ltm_read_media", "ltm_search", "ltm_store"),
            names,
        )
        runtime.tools.forEach { tool ->
            assertTrue(
                "every ltm tool must be an LtmToolAdapter, got ${tool.javaClass.simpleName}",
                tool is LtmToolAdapter,
            )
        }
    }

    @Test
    fun `enabled mem with missing base urls falls back to local llm settings`() {
        // MemSettings leaves base URLs null; MemConfigResolver falls back to
        // LocalLlmSettings.baseUrl. With both set, the factory must still
        // produce the four tools.
        val settings = MemSettings(enabled = true, embeddingDimensions = 8)
        val localLlm = localLlmSettings(baseUrl = "https://fallback.example.com/v1")

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        assertEquals(5, runtime.tools.size)
        assertNotNull("store.load() job must be launched when mem is enabled", runtime.lastLoadJob)
    }

    @Test
    fun `enabled mem with no resolvable base url produces zero tools`() {
        // Neither MemSettings nor LocalLlmSettings provides a base URL →
        // MemConfigResolver returns null → factory returns an empty tool list.
        val settings = MemSettings(enabled = true, embeddingDimensions = 8)
        val localLlm = LocalLlmSettings(enabled = true) // baseUrl defaults to null/blank

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        assertTrue("unresolvable config must register zero tools", runtime.tools.isEmpty())
        assertEquals(null, runtime.lastLoadJob)
        assertNull("unresolvable config must expose no pipeline", runtime.pipeline)
        assertNull("unresolvable config must expose no store", runtime.store)
        assertNull("unresolvable config must expose no retrieval", runtime.retrieval)
    }

    @Test
    fun `store load job completes against a real database`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extraction.example.com/v1",
            embeddingBaseUrl = "https://embedding.example.com/v1",
            embeddingDimensions = 8,
        )
        val localLlm = localLlmSettings()

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        val job = runtime.lastLoadJob
        assertNotNull("load job must be launched", job)
        // Dispatchers.Unconfined runs the coroutine inline, so by the time
        // createLtmTools returns the load has already completed.
        runBlocking { job!!.join() }
        assertTrue("load job must complete successfully", job!!.isCompleted)
    }

    @Test
    fun `enabled mem exposes non-null pipeline store and retrieval`() {
        // T1/T5 wiring contract: the host (ChatWebSocketManager) reads pipeline/store/retrieval
        // off MemRuntime to drive the compact→ingest trigger, the life_capture ingest, and the
        // ltm_read_media byte callback. These MUST be non-null when mem is enabled, and the
        // pipeline must be the same ExtractionPipeline the ltm_store tool ingests through.
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extraction.example.com/v1",
            embeddingBaseUrl = "https://embedding.example.com/v1",
            embeddingDimensions = 8,
        )
        val localLlm = localLlmSettings()

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        assertNotNull("pipeline must be exposed when mem is enabled", runtime.pipeline)
        assertNotNull("store must be exposed when mem is enabled", runtime.store)
        assertNotNull("retrieval must be exposed when mem is enabled", runtime.retrieval)
    }

    @Test
    fun `ltm tool definitions carry the mem schema parameters`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extraction.example.com/v1",
            embeddingBaseUrl = "https://embedding.example.com/v1",
            embeddingDimensions = 8,
        )
        val localLlm = localLlmSettings()

        val runtime = MemRuntimeFactory.createLtmTools(
            scope = scope,
            memSettings = settings,
            localLlmSettings = localLlm,
            database = db,
        )

        val search = runtime.tools.first { it.definition.name == "ltm_search" }
        val params = search.definition.parameters
        assertEquals("object", params["type"])
        val properties = params["properties"] as Map<*, *>
        assertNotNull(properties!!["query"])
        assertNotNull(properties["k"])
        assertNotNull(properties["time_range"])
        assertEquals(false, params["additionalProperties"])
    }

    /* ------------------------------------------------------------------ */
    /* Helpers                                                            */
    /* ------------------------------------------------------------------ */

    private fun localLlmSettings(baseUrl: String? = "https://chat.example.com/v1"): LocalLlmSettings =
        LocalLlmSettings(
            enabled = true,
            baseUrl = baseUrl,
            apiKey = "chat-key",
            plannerModel = "chat-model",
        )
}
