package com.l2dchat.core.config

import android.content.SharedPreferences
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MemSettingsStoreTest {
    @Test
    fun `read returns defaults when prefs are empty`() {
        val prefs = InMemorySharedPreferences()
        val secrets = InMemoryMemSecretStore()

        val settings = MemSettingsStore.read(prefs, secrets)

        assertFalse(settings.enabled)
        assertNull(settings.extractionBaseUrl)
        assertNull(settings.extractionApiKey)
        // Model ids fall back to BuildConfig defaults baked into MemSettings.DEFAULT_*.
        assertEquals(MemSettings.DEFAULT_EXTRACTION_MODEL, settings.extractionModel)
        assertEquals(MemSettings.DEFAULT_EXTRACTION_MM_MODEL, settings.extractionMmModel)
        assertEquals(MemSettings.DEFAULT_EMBEDDING_MODEL, settings.embeddingModel)
        assertNull(settings.embeddingBaseUrl)
        assertNull(settings.embeddingApiKey)
        assertEquals(MemSettings.DEFAULT_EMBEDDING_DIMENSIONS, settings.embeddingDimensions)
    }

    @Test
    fun `persist then read round-trips all populated fields`() {
        val prefs = InMemorySharedPreferences()
        val secrets = InMemoryMemSecretStore()
        val original =
                MemSettings(
                        enabled = true,
                        extractionBaseUrl = "https://extraction.example.com/v1",
                        extractionApiKey = "extraction-secret",
                        extractionModel = "extraction-model",
                        extractionMmModel = "extraction-mm-model",
                        embeddingBaseUrl = "https://embedding.example.com/v1",
                        embeddingApiKey = "embedding-secret",
                        embeddingModel = "embedding-model",
                        embeddingDimensions = 768
                )

        assertTrue(MemSettingsStore.persist(prefs, secrets, original))
        val roundTripped = MemSettingsStore.read(prefs, secrets)

        assertEquals(original, roundTripped)
    }

    @Test
    fun `persist writes both api keys only to the secret store`() {
        val prefs = InMemorySharedPreferences()
        val secrets = InMemoryMemSecretStore()
        val settings =
                MemSettings(
                        enabled = true,
                        extractionApiKey = "extraction-secret",
                        embeddingApiKey = "embedding-secret"
                )

        MemSettingsStore.persist(prefs, secrets, settings)

        assertEquals(
                "extraction-secret",
                secrets.values[MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY]
        )
        assertEquals(
                "embedding-secret",
                secrets.values[MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY]
        )
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY))
    }

    @Test
    fun `persist removes legacy plaintext api keys from regular prefs`() {
        val prefs =
                InMemorySharedPreferences(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "legacy-extraction",
                        MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY to "legacy-embedding"
                )
        val secrets = InMemoryMemSecretStore()

        MemSettingsStore.persist(
                prefs,
                secrets,
                MemSettings(enabled = true, extractionApiKey = "new-extraction", embeddingApiKey = null)
        )

        assertEquals(
                "new-extraction",
                secrets.values[MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY]
        )
        // Blank embedding key clears the secret.
        assertNull(secrets.values[MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY])
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY))
    }

    @Test
    fun `read migrates legacy plaintext api keys into the secret store`() {
        val prefs =
                InMemorySharedPreferences(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "legacy-extraction",
                        MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY to "legacy-embedding"
                )
        val secrets = InMemoryMemSecretStore()

        val settings = MemSettingsStore.read(prefs, secrets)

        assertEquals("legacy-extraction", settings.extractionApiKey)
        assertEquals("legacy-embedding", settings.embeddingApiKey)
        assertEquals(
                "legacy-extraction",
                secrets.values[MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY]
        )
        assertEquals(
                "legacy-embedding",
                secrets.values[MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY]
        )
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY))
    }

    @Test
    fun `read prefers secure api key over legacy plaintext`() {
        val prefs =
                InMemorySharedPreferences(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "legacy-extraction"
                )
        val secrets =
                InMemoryMemSecretStore(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "secure-extraction"
                )

        val settings = MemSettingsStore.read(prefs, secrets)

        assertEquals("secure-extraction", settings.extractionApiKey)
        assertEquals(
                "secure-extraction",
                secrets.values[MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY]
        )
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY))
    }

    @Test
    fun `read retains legacy plaintext when secure migration fails`() {
        val prefs =
                InMemorySharedPreferences(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "legacy-extraction"
                )
        val secrets = FailingMemSecretStore()

        val settings = MemSettingsStore.read(prefs, secrets)

        assertEquals("legacy-extraction", settings.extractionApiKey)
        assertTrue(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY))
        assertEquals(
                "legacy-extraction",
                prefs.getString(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY, null)
        )
    }

    @Test
    fun `persist reports secure write failure and retains legacy plaintext keys`() {
        val prefs =
                InMemorySharedPreferences(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "legacy-extraction",
                        MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY to "legacy-embedding"
                )
        val secrets = FailingMemSecretStore()

        val persisted =
                MemSettingsStore.persist(
                        prefs,
                        secrets,
                        MemSettings(
                                enabled = true,
                                extractionApiKey = "new-extraction",
                                embeddingApiKey = "new-embedding"
                        )
                )

        assertFalse(persisted)
        assertEquals(
                "legacy-extraction",
                prefs.getString(MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY, null)
        )
        assertEquals(
                "legacy-embedding",
                prefs.getString(MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY, null)
        )
    }

    @Test
    fun `persist clears blank optional fields and falls back to defaults on read`() {
        val prefs =
                InMemorySharedPreferences(
                        MemSettingsStore.KEY_MEM_EXTRACTION_BASE_URL to "https://old.example.com",
                        MemSettingsStore.KEY_MEM_EXTRACTION_MODEL to "old-extraction",
                        MemSettingsStore.KEY_MEM_EXTRACTION_MM_MODEL to "old-mm",
                        MemSettingsStore.KEY_MEM_EMBEDDING_BASE_URL to "https://old-emb.example.com",
                        MemSettingsStore.KEY_MEM_EMBEDDING_MODEL to "old-emb",
                        MemSettingsStore.KEY_MEM_EMBEDDING_DIMENSIONS to 512
                )
        val secrets =
                InMemoryMemSecretStore(
                        MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY to "old-extraction-secret",
                        MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY to "old-emb-secret"
                )

        MemSettingsStore.persist(
                prefs,
                secrets,
                MemSettings(
                        enabled = false,
                        extractionBaseUrl = " ",
                        extractionApiKey = " ",
                        extractionModel = null,
                        extractionMmModel = null,
                        embeddingBaseUrl = null,
                        embeddingApiKey = null,
                        embeddingModel = null,
                        embeddingDimensions = MemSettings.DEFAULT_EMBEDDING_DIMENSIONS
                )
        )

        // Blank secrets are removed.
        assertNull(secrets.values[MemSettingsStore.KEY_MEM_EXTRACTION_API_KEY])
        assertNull(secrets.values[MemSettingsStore.KEY_MEM_EMBEDDING_API_KEY])
        // Blank optional fields are removed from prefs.
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_BASE_URL))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_MODEL))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EXTRACTION_MM_MODEL))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EMBEDDING_BASE_URL))
        assertFalse(prefs.contains(MemSettingsStore.KEY_MEM_EMBEDDING_MODEL))
        assertFalse(prefs.getBoolean(MemSettingsStore.KEY_MEM_ENABLED, true))

        val reread = MemSettingsStore.read(prefs, secrets)
        // Nullable fields fall back to BuildConfig defaults (or null for base urls / api keys).
        assertNull(reread.extractionBaseUrl)
        assertNull(reread.extractionApiKey)
        assertEquals(MemSettings.DEFAULT_EXTRACTION_MODEL, reread.extractionModel)
        assertEquals(MemSettings.DEFAULT_EXTRACTION_MM_MODEL, reread.extractionMmModel)
        assertNull(reread.embeddingBaseUrl)
        assertNull(reread.embeddingApiKey)
        assertEquals(MemSettings.DEFAULT_EMBEDDING_MODEL, reread.embeddingModel)
        assertEquals(MemSettings.DEFAULT_EMBEDDING_DIMENSIONS, reread.embeddingDimensions)
    }

    @Test
    fun `persist writes custom embedding dimensions and round-trips them`() {
        val prefs = InMemorySharedPreferences()
        val secrets = InMemoryMemSecretStore()

        MemSettingsStore.persist(
                prefs,
                secrets,
                MemSettings(enabled = true, embeddingDimensions = 1536)
        )

        assertEquals(
                1536,
                prefs.getInt(MemSettingsStore.KEY_MEM_EMBEDDING_DIMENSIONS, 0)
        )

        val reread = MemSettingsStore.read(prefs, secrets)
        assertEquals(1536, reread.embeddingDimensions)
    }

    @Test
    fun `toString redacts both api keys`() {
        val text =
                MemSettings(
                        enabled = true,
                        extractionApiKey = "extraction-secret",
                        embeddingApiKey = "embedding-secret"
                ).toString()

        assertFalse(text.contains("extraction-secret"))
        assertFalse(text.contains("embedding-secret"))
        assertTrue(text.contains("extractionApiKey=<redacted>"))
        assertTrue(text.contains("embeddingApiKey=<redacted>"))
    }
}

private class InMemoryMemSecretStore(vararg entries: Pair<String, String>) : MemSecretStore {
    val values = entries.toMap().toMutableMap()

    override fun getString(key: String): String? = values[key]

    override fun putString(key: String, value: String?): Boolean {
        if (value.isNullOrBlank()) values.remove(key) else values[key] = value
        return true
    }
}

private class FailingMemSecretStore : MemSecretStore {
    override fun getString(key: String): String? = null

    override fun putString(key: String, value: String?): Boolean = false
}

private class InMemorySharedPreferences(vararg entries: Pair<String, Any?>) : SharedPreferences {
    private val values = entries.toMap().toMutableMap()

    override fun getAll(): MutableMap<String, *> = values.toMutableMap()

    override fun getString(key: String?, defValue: String?): String? =
            values[key] as? String ?: defValue

    @Suppress("UNCHECKED_CAST")
    override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? =
            (values[key] as? Set<String>)?.toMutableSet() ?: defValues

    override fun getInt(key: String?, defValue: Int): Int = values[key] as? Int ?: defValue

    override fun getLong(key: String?, defValue: Long): Long = values[key] as? Long ?: defValue

    override fun getFloat(key: String?, defValue: Float): Float = values[key] as? Float ?: defValue

    override fun getBoolean(key: String?, defValue: Boolean): Boolean =
            values[key] as? Boolean ?: defValue

    override fun contains(key: String?): Boolean = values.containsKey(key)

    override fun edit(): SharedPreferences.Editor = Editor()

    override fun registerOnSharedPreferenceChangeListener(
            listener: SharedPreferences.OnSharedPreferenceChangeListener?
    ) = Unit

    override fun unregisterOnSharedPreferenceChangeListener(
            listener: SharedPreferences.OnSharedPreferenceChangeListener?
    ) = Unit

    private inner class Editor : SharedPreferences.Editor {
        private val changes = linkedMapOf<String, Any?>()
        private val removals = mutableSetOf<String>()
        private var clear = false

        override fun putString(key: String?, value: String?): SharedPreferences.Editor =
                putNullable(key, value)

        override fun putStringSet(
                key: String?,
                values: MutableSet<String>?
        ): SharedPreferences.Editor = putNullable(key, values?.toSet())

        override fun putInt(key: String?, value: Int): SharedPreferences.Editor = put(key, value)

        override fun putLong(key: String?, value: Long): SharedPreferences.Editor = put(key, value)

        override fun putFloat(key: String?, value: Float): SharedPreferences.Editor =
                put(key, value)

        override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor =
                put(key, value)

        override fun remove(key: String?): SharedPreferences.Editor {
            key?.let {
                removals += it
                changes.remove(it)
            }
            return this
        }

        override fun clear(): SharedPreferences.Editor {
            clear = true
            changes.clear()
            removals.clear()
            return this
        }

        override fun commit(): Boolean {
            if (clear) values.clear()
            removals.forEach { values.remove(it) }
            changes.forEach { (key, value) ->
                if (value == null) values.remove(key) else values[key] = value
            }
            return true
        }

        override fun apply() {
            commit()
        }

        private fun put(key: String?, value: Any): SharedPreferences.Editor {
            key?.let {
                changes[it] = value
                removals.remove(it)
            }
            return this
        }

        private fun putNullable(key: String?, value: Any?): SharedPreferences.Editor {
            key?.let {
                changes[it] = value
                removals.remove(it)
            }
            return this
        }
    }
}
