package com.l2dchat.core.mem

import com.l2dchat.BuildConfig
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.MemSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Verifies [MemConfigResolver] correctly merges [MemSettings] + [LocalLlmSettings]
 * + [BuildConfig] defaults into a fully-resolved [MemLlmConfig], and returns null
 * when mem is disabled or no base URL can be resolved.
 *
 * Coverage (per plan T2.5 checklist):
 * - Full config resolves with all model → provider references intact.
 * - Embedding base URL falls back to chat base URL when mem embedding base URL is unset.
 * - Extraction base URL falls back to chat base URL when mem extraction base URL is unset.
 * - All-missing base URLs → null.
 * - Disabled → null.
 * - Extraction API key falls back to chat API key.
 * - Embedding API key stays null when unset (no chat-key fallback).
 * - Model identifiers fall back to BuildConfig defaults.
 */
class MemConfigResolverTest {

    @Test
    fun `full config resolves and every model maps to its provider`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extract.test/v1",
            extractionApiKey = "extract-key",
            extractionModel = "extraction-id",
            extractionMmModel = "extraction-mm-id",
            embeddingBaseUrl = "https://embed.test/v1",
            embeddingApiKey = "embed-key",
            embeddingModel = "embed-id",
            embeddingDimensions = 768,
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = "chat-key")

        val config = MemConfigResolver.resolve(settings, local)

        assertNotNull(config)
        val resolved = config!!
        resolved.extractionModel().let { model ->
            assertEquals("extraction-id", model.modelIdentifier)
            assertEquals(ModelKind.CHAT, model.kind)
            assertEquals("mem_extraction", resolved.providerFor(model).name)
            assertEquals("https://extract.test/v1", resolved.providerFor(model).baseUrl)
            assertEquals("extract-key", resolved.providerFor(model).apiKey)
        }
        resolved.extractionMmModel().let { model ->
            assertEquals("extraction-mm-id", model.modelIdentifier)
            assertEquals("mem_extraction", resolved.providerFor(model).name)
        }
        resolved.embeddingModel().let { model ->
            assertEquals("embed-id", model.modelIdentifier)
            assertEquals(ModelKind.EMBEDDING, model.kind)
            assertEquals(768, model.dimensions)
            assertEquals("mem_embedding", resolved.providerFor(model).name)
            assertEquals("https://embed.test/v1", resolved.providerFor(model).baseUrl)
            assertEquals("embed-key", resolved.providerFor(model).apiKey)
        }
    }

    @Test
    fun `embedding baseUrl falls back to chat baseUrl when mem embedding baseUrl is unset`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extract.test/v1",
            embeddingBaseUrl = null,
            embeddingModel = "embed-id",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = "chat-key")

        val config = MemConfigResolver.resolve(settings, local)

        assertNotNull(config)
        assertEquals(
            "https://chat.test/v1",
            config!!.providerFor(config.embeddingModel()).baseUrl,
        )
    }

    @Test
    fun `extraction baseUrl falls back to chat baseUrl when mem extraction baseUrl is unset`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = null,
            embeddingBaseUrl = "https://embed.test/v1",
            extractionModel = "extraction-id",
            embeddingModel = "embed-id",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = null)

        val config = MemConfigResolver.resolve(settings, local)

        assertNotNull(config)
        assertEquals(
            "https://chat.test/v1",
            config!!.providerFor(config.extractionModel()).baseUrl,
        )
    }

    @Test
    fun `all baseUrls missing returns null`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = null,
            embeddingBaseUrl = null,
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = null, apiKey = null)

        val config = MemConfigResolver.resolve(settings, local)

        assertNull(config)
    }

    @Test
    fun `disabled mem returns null even when baseUrls are present`() {
        val settings = MemSettings(
            enabled = false,
            extractionBaseUrl = "https://extract.test/v1",
            embeddingBaseUrl = "https://embed.test/v1",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = "chat-key")

        assertNull(MemConfigResolver.resolve(settings, local))
    }

    @Test
    fun `extraction apiKey falls back to chat apiKey`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extract.test/v1",
            extractionApiKey = null,
            embeddingBaseUrl = "https://embed.test/v1",
            extractionModel = "extraction-id",
            embeddingModel = "embed-id",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = "chat-key")

        val config = MemConfigResolver.resolve(settings, local)

        assertNotNull(config)
        assertEquals(
            "chat-key",
            config!!.providerFor(config.extractionModel()).apiKey,
        )
    }

    @Test
    fun `embedding apiKey stays null when unset even if chat apiKey is present`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extract.test/v1",
            embeddingBaseUrl = "https://embed.test/v1",
            extractionApiKey = null,
            embeddingApiKey = null,
            extractionModel = "extraction-id",
            embeddingModel = "embed-id",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = "chat-key")

        val config = MemConfigResolver.resolve(settings, local)

        assertNotNull(config)
        assertNull(config!!.providerFor(config.embeddingModel()).apiKey)
    }

    @Test
    fun `model identifiers fall back to BuildConfig defaults when MemSettings leaves them unset`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extract.test/v1",
            embeddingBaseUrl = "https://embed.test/v1",
            extractionModel = null,
            extractionMmModel = null,
            embeddingModel = null,
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = null)

        val config = MemConfigResolver.resolve(settings, local)

        assertNotNull(config)
        val resolved = config!!
        assertEquals(BuildConfig.MEM_DEFAULT_EXTRACTION_MODEL, resolved.extractionModel().modelIdentifier)
        assertEquals(BuildConfig.MEM_DEFAULT_EXTRACTION_MM_MODEL, resolved.extractionMmModel().modelIdentifier)
        assertEquals(BuildConfig.MEM_DEFAULT_EMBEDDING_MODEL, resolved.embeddingModel().modelIdentifier)
    }

    @Test
    fun `blank strings are treated as unset`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "   ",
            embeddingBaseUrl = "   ",
            extractionApiKey = "  ",
            embeddingApiKey = "  ",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "  ", apiKey = "  ")

        assertNull(MemConfigResolver.resolve(settings, local))
    }

    @Test
    fun `resolved config is usable by MemLlmClient without throwing`() {
        val settings = MemSettings(
            enabled = true,
            extractionBaseUrl = "https://extract.test/v1",
            embeddingBaseUrl = "https://embed.test/v1",
            extractionModel = "extraction-id",
            extractionMmModel = "extraction-mm-id",
            embeddingModel = "embed-id",
        )
        val local = LocalLlmSettings(enabled = true, baseUrl = "https://chat.test/v1", apiKey = null)

        val config = MemConfigResolver.resolve(settings, local)!!

        val memClient = MemLlmClient(config)
        assertEquals("extraction-id", memClient.extractionConfig().model)
        assertEquals("extraction-mm-id", memClient.extractionConfig(multimodal = true).model)
        assertTrue(config.embeddingModel().dimensions!! > 0)
    }
}
