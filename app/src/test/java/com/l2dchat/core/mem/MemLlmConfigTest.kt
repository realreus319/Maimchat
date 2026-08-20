package com.l2dchat.core.mem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Covers [MemLlmConfig] resolution: the happy path and the two dangling-reference
 * failures (missing model, model pointing at an unknown provider).
 *
 * Fixtures mirror the shape of `config/mem.toml` from the Python reference so
 * the test doubles as a structural check that the Kotlin model can express the
 * real production config.
 */
class MemLlmConfigTest {
    @Test
    fun `full config resolves extraction, extraction_mm, embedding models and providers`() {
        val config = fullConfig()

        val extraction = config.extractionModel()
        assertEquals("qwen37max", extraction.name)
        assertEquals("ali", extraction.provider)
        assertEquals("qwen3.7-max", extraction.modelIdentifier)
        assertEquals(ModelKind.CHAT, extraction.kind)
        assertEquals(8192, extraction.maxTokens)
        assertEquals(0.3, extraction.temperature!!, 0.0)

        val extractionMm = config.extractionMmModel()
        assertEquals("qwen37plus", extractionMm.name)
        assertEquals("qwen3.7-plus", extractionMm.modelIdentifier)
        assertEquals(ModelKind.CHAT, extractionMm.kind)

        val embedding = config.embeddingModel()
        assertEquals("local-embed", embedding.name)
        assertEquals("local", embedding.provider)
        assertEquals("Qwen/Qwen3-Embedding-0.6B", embedding.modelIdentifier)
        assertEquals(ModelKind.EMBEDDING, embedding.kind)
        assertEquals(1024, embedding.dimensions)

        val aliProvider = config.providerFor(extraction)
        assertEquals("ali", aliProvider.name)
        assertEquals("https://dashscope.aliyuncs.com/compatible-mode/v1", aliProvider.baseUrl)
        assertEquals("ali-key", aliProvider.apiKey)
        assertEquals(120, aliProvider.timeoutSeconds)

        val localProvider = config.providerFor(embedding)
        assertEquals("local", localProvider.name)
        assertEquals("http://localhost:8010/v1", localProvider.baseUrl)
        // Key-less provider stays null — the host OpenAI client treats blank/null keys as
        // "no Authorization header" rather than failing.
        assertEquals(null, localProvider.apiKey)
        assertEquals(60, localProvider.timeoutSeconds)
    }

    @Test
    fun `extractionModel throws ConfigError when the referenced model is absent`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models = emptyMap(),
                tasks =
                    MemTaskConfig(
                        extraction = "ghost",
                        extractionMm = "ghost-mm",
                        embedding = "ghost-embed",
                    ),
            )

        try {
            config.extractionModel()
            fail("Expected ConfigError for missing extraction model")
        } catch (e: ConfigError) {
            assertTrue(
                "Message must name the missing model: ${e.message}",
                e.message?.contains("ghost") == true,
            )
            assertTrue("Must be a MemError", e is MemError)
        }
    }

    @Test
    fun `extractionMmModel throws ConfigError when the referenced model is absent`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models =
                    mapOf(
                        "qwen37max" to
                            MemModel(
                                name = "qwen37max",
                                provider = "ali",
                                modelIdentifier = "qwen3.7-max",
                                kind = ModelKind.CHAT,
                            )
                    ),
                tasks =
                    MemTaskConfig(
                        extraction = "qwen37max",
                        extractionMm = "no-such-mm",
                        embedding = "no-such-embed",
                    ),
            )

        try {
            config.extractionMmModel()
            fail("Expected ConfigError for missing extraction_mm model")
        } catch (e: ConfigError) {
            assertNotNull(e.message)
            assertTrue(e.message!!.contains("no-such-mm"))
        }
    }

    @Test
    fun `embeddingModel throws ConfigError when the referenced model is absent`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models = emptyMap(),
                tasks =
                    MemTaskConfig(
                        extraction = "any-extraction",
                        extractionMm = "any-mm",
                        embedding = "ghost-embed",
                    ),
            )

        try {
            config.embeddingModel()
            fail("Expected ConfigError for missing embedding model")
        } catch (e: ConfigError) {
            assertTrue(e.message!!.contains("ghost-embed"))
        }
    }

    @Test
    fun `providerFor throws ConfigError when the model references an unknown provider`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models =
                    mapOf(
                        "orphan" to
                            MemModel(
                                name = "orphan",
                                provider = "ghost-provider",
                                modelIdentifier = "orphan-id",
                                kind = ModelKind.CHAT,
                            )
                    ),
                tasks =
                    MemTaskConfig(
                        extraction = "orphan",
                        extractionMm = "orphan",
                        embedding = "orphan",
                    ),
            )

        // Extraction model resolves, but joining its provider must fail.
        val model = config.extractionModel()
        assertEquals("orphan", model.name)
        try {
            config.providerFor(model)
            fail("Expected ConfigError for unknown provider")
        } catch (e: ConfigError) {
            assertTrue(
                "Message must name the missing provider: ${e.message}",
                e.message?.contains("ghost-provider") == true,
            )
            assertTrue(
                "Message should also name the referencing model for diagnostics: ${e.message}",
                e.message?.contains("orphan") == true,
            )
        }
    }

    @Test
    fun `MemProvider rejects blank name and baseUrl`() {
        try {
            MemProvider(name = "", baseUrl = "https://example.test/v1")
            fail("Expected IllegalArgumentException for blank name")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("name"))
        }
        try {
            MemProvider(name = "ali", baseUrl = "")
            fail("Expected IllegalArgumentException for blank baseUrl")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("baseUrl"))
        }
    }

    @Test
    fun `MemProvider rejects non-positive timeout`() {
        try {
            MemProvider(name = "ali", baseUrl = "https://example.test/v1", timeoutSeconds = 0)
            fail("Expected IllegalArgumentException for zero timeout")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("timeoutSeconds"))
        }
    }

    @Test
    fun `MemModel rejects blank name, provider, modelIdentifier`() {
        try {
            MemModel(name = "", provider = "ali", modelIdentifier = "id", kind = ModelKind.CHAT)
            fail("Expected IllegalArgumentException for blank name")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("name"))
        }
        try {
            MemModel(name = "m", provider = "", modelIdentifier = "id", kind = ModelKind.CHAT)
            fail("Expected IllegalArgumentException for blank provider")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("provider"))
        }
        try {
            MemModel(name = "m", provider = "ali", modelIdentifier = "", kind = ModelKind.CHAT)
            fail("Expected IllegalArgumentException for blank modelIdentifier")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("modelIdentifier"))
        }
    }

    @Test
    fun `MemModel rejects out-of-range temperature and non-positive maxTokens`() {
        try {
            MemModel(
                name = "m",
                provider = "ali",
                modelIdentifier = "id",
                kind = ModelKind.CHAT,
                temperature = 3.0,
            )
            fail("Expected IllegalArgumentException for temperature > 2")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("temperature"))
        }
        try {
            MemModel(
                name = "m",
                provider = "ali",
                modelIdentifier = "id",
                kind = ModelKind.CHAT,
                maxTokens = 0,
            )
            fail("Expected IllegalArgumentException for maxTokens = 0")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("maxTokens"))
        }
    }

    @Test
    fun `MemTaskConfig rejects blank required task entries`() {
        try {
            MemTaskConfig(extraction = "", extractionMm = "mm", embedding = "emb")
            fail("Expected IllegalArgumentException for blank extraction")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("extraction"))
        }
        try {
            MemTaskConfig(extraction = "ex", extractionMm = "", embedding = "emb")
            fail("Expected IllegalArgumentException for blank extractionMm")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("extractionMm"))
        }
        try {
            MemTaskConfig(extraction = "ex", extractionMm = "mm", embedding = "")
            fail("Expected IllegalArgumentException for blank embedding")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("embedding"))
        }
    }

    @Test
    fun `vision and websearch pass-through tasks are carried without affecting resolution`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models =
                    mapOf(
                        "qwen37max" to
                            MemModel(
                                name = "qwen37max",
                                provider = "ali",
                                modelIdentifier = "qwen3.7-max",
                                kind = ModelKind.CHAT,
                            ),
                        "qwen37plus" to
                            MemModel(
                                name = "qwen37plus",
                                provider = "ali",
                                modelIdentifier = "qwen3.7-plus",
                                kind = ModelKind.CHAT,
                            ),
                        "local-embed" to
                            MemModel(
                                name = "local-embed",
                                provider = "ali",
                                modelIdentifier = "emb-id",
                                kind = ModelKind.EMBEDDING,
                                dimensions = 1024,
                            ),
                        "vl" to
                            MemModel(
                                name = "vl",
                                provider = "ali",
                                modelIdentifier = "vl-id",
                                kind = ModelKind.CHAT,
                            ),
                    ),
                tasks =
                    MemTaskConfig(
                        extraction = "qwen37max",
                        extractionMm = "qwen37plus",
                        embedding = "local-embed",
                        vision = "vl",
                        websearch = "qwen37max",
                    ),
            )

        assertEquals("vl", config.tasks.vision)
        assertEquals("qwen37max", config.tasks.websearch)
        // Resolution paths are unaffected by the extra entries.
        assertEquals("qwen3.7-max", config.extractionModel().modelIdentifier)
        assertEquals("emb-id", config.embeddingModel().modelIdentifier)
    }

    private fun fullConfig(): MemLlmConfig =
        MemLlmConfig(
            providers =
                mapOf(
                    "ali" to
                        MemProvider(
                            name = "ali",
                            baseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            apiKey = "ali-key",
                            timeoutSeconds = 120,
                        ),
                    "local" to
                        MemProvider(
                            name = "local",
                            baseUrl = "http://localhost:8010/v1",
                            apiKey = null,
                            timeoutSeconds = 60,
                        ),
                ),
            models =
                mapOf(
                    "qwen37plus" to
                        MemModel(
                            name = "qwen37plus",
                            provider = "ali",
                            modelIdentifier = "qwen3.7-plus",
                            kind = ModelKind.CHAT,
                            maxTokens = 8192,
                            temperature = 0.3,
                        ),
                    "qwen37max" to
                        MemModel(
                            name = "qwen37max",
                            provider = "ali",
                            modelIdentifier = "qwen3.7-max",
                            kind = ModelKind.CHAT,
                            maxTokens = 8192,
                            temperature = 0.3,
                        ),
                    "ali-embed" to
                        MemModel(
                            name = "ali-embed",
                            provider = "ali",
                            modelIdentifier = "qwen3.7-text-embedding",
                            kind = ModelKind.EMBEDDING,
                            dimensions = 1024,
                        ),
                    "local-embed" to
                        MemModel(
                            name = "local-embed",
                            provider = "local",
                            modelIdentifier = "Qwen/Qwen3-Embedding-0.6B",
                            kind = ModelKind.EMBEDDING,
                            dimensions = 1024,
                        ),
                ),
            tasks =
                MemTaskConfig(
                    extraction = "qwen37max",
                    extractionMm = "qwen37plus",
                    embedding = "local-embed",
                    vision = "qwen37plus",
                    websearch = "qwen37plus",
                ),
        )
}
