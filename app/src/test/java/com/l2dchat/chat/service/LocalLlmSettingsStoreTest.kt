package com.l2dchat.chat.service

import android.content.SharedPreferences
import com.l2dchat.core.config.LocalLlmSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalLlmSettingsStoreTest {
    @Test
    fun `read migrates legacy api key into secure store`() {
        val prefs =
                InMemorySharedPreferences(
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_ENABLED to true,
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_BASE_URL to "https://api.example.com/v1",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY to "legacy-key",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_PLANNER_MODEL to "planner",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_REPLIER_MODEL to "replier",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_NATIVE_TOOL_CALLING to false,
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_TEMPERATURE to "0.25",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_MAX_TOKENS to 512,
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_TIMEOUT_MILLIS to 30_000L
                )
        val secrets = InMemorySecretStringStore()

        val settings = LocalLlmSettingsStore.read(prefs, secrets)

        assertTrue(settings.enabled)
        assertEquals("https://api.example.com/v1", settings.baseUrl)
        assertEquals("legacy-key", settings.apiKey)
        assertEquals("planner", settings.plannerModel)
        assertEquals("replier", settings.replierModel)
        assertFalse(settings.nativeToolCalling)
        assertEquals(0.25, settings.temperature)
        assertEquals(512, settings.maxTokens)
        assertEquals(30_000L, settings.timeoutMillis)
        assertEquals("legacy-key", secrets.values[LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY])
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY))
    }

    @Test
    fun `read prefers secure api key and removes stale plaintext`() {
        val prefs =
                InMemorySharedPreferences(
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY to "legacy-key"
                )
        val secrets =
                InMemorySecretStringStore(
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY to "secure-key"
                )

        val settings = LocalLlmSettingsStore.read(prefs, secrets)

        assertEquals("secure-key", settings.apiKey)
        assertEquals("secure-key", secrets.values[LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY])
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY))
    }

    @Test
    fun `persist writes api key only to secure store`() {
        val prefs =
                InMemorySharedPreferences(
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY to "old-plaintext"
                )
        val secrets = InMemorySecretStringStore()
        val settings =
                LocalLlmSettings(
                        enabled = true,
                        baseUrl = "https://api.example.com/v1",
                        apiKey = "new-key",
                        plannerModel = "planner",
                        replierModel = "replier",
                        nativeToolCalling = false,
                        temperature = 0.8,
                        maxTokens = 2048,
                        timeoutMillis = 45_000L
                )

        LocalLlmSettingsStore.persist(prefs, secrets, settings)

        assertEquals("new-key", secrets.values[LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY])
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY))
        assertTrue(prefs.getBoolean(LocalLlmSettingsStore.KEY_LOCAL_LLM_ENABLED, false))
        assertEquals(
                "https://api.example.com/v1",
                prefs.getString(LocalLlmSettingsStore.KEY_LOCAL_LLM_BASE_URL, null)
        )
        assertEquals("planner", prefs.getString(LocalLlmSettingsStore.KEY_LOCAL_LLM_PLANNER_MODEL, null))
        assertEquals("replier", prefs.getString(LocalLlmSettingsStore.KEY_LOCAL_LLM_REPLIER_MODEL, null))
        assertFalse(prefs.getBoolean(LocalLlmSettingsStore.KEY_LOCAL_LLM_NATIVE_TOOL_CALLING, true))
        assertEquals("0.8", prefs.getString(LocalLlmSettingsStore.KEY_LOCAL_LLM_TEMPERATURE, null))
        assertEquals(2048, prefs.getInt(LocalLlmSettingsStore.KEY_LOCAL_LLM_MAX_TOKENS, 0))
        assertEquals(45_000L, prefs.getLong(LocalLlmSettingsStore.KEY_LOCAL_LLM_TIMEOUT_MILLIS, 0L))
    }

    @Test
    fun `persist clears blank api key and optional provider fields`() {
        val prefs =
                InMemorySharedPreferences(
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_BASE_URL to "https://old.example.com",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_PLANNER_MODEL to "old-planner",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_REPLIER_MODEL to "old-replier",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_TEMPERATURE to "0.1",
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_MAX_TOKENS to 128,
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_TIMEOUT_MILLIS to 10_000L
                )
        val secrets =
                InMemorySecretStringStore(
                        LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY to "old-secret"
                )

        LocalLlmSettingsStore.persist(
                prefs,
                secrets,
                LocalLlmSettings(enabled = false, baseUrl = " ", apiKey = " ")
        )

        assertNull(secrets.values[LocalLlmSettingsStore.KEY_LOCAL_LLM_API_KEY])
        assertFalse(prefs.getBoolean(LocalLlmSettingsStore.KEY_LOCAL_LLM_ENABLED, true))
        assertTrue(prefs.getBoolean(LocalLlmSettingsStore.KEY_LOCAL_LLM_NATIVE_TOOL_CALLING, false))
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_BASE_URL))
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_PLANNER_MODEL))
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_REPLIER_MODEL))
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_TEMPERATURE))
        assertFalse(prefs.contains(LocalLlmSettingsStore.KEY_LOCAL_LLM_MAX_TOKENS))
        assertEquals(
                LocalLlmSettings.DEFAULT_TIMEOUT_MILLIS,
                prefs.getLong(LocalLlmSettingsStore.KEY_LOCAL_LLM_TIMEOUT_MILLIS, 0L)
        )
    }

    @Test
    fun `runtime mode defaults to local for fresh install`() {
        val prefs = InMemorySharedPreferences()

        val mode =
                ChatRuntimeModeStore.read(
                        prefs = prefs,
                        legacyLocalLlmEnabled = false,
                        legacyRemoteUrl = null
                )

        assertEquals(ChatRuntimeMode.LOCAL, mode)
    }

    @Test
    fun `runtime mode preserves legacy remote websocket installs`() {
        val prefs = InMemorySharedPreferences()

        val mode =
                ChatRuntimeModeStore.read(
                        prefs = prefs,
                        legacyLocalLlmEnabled = false,
                        legacyRemoteUrl = "ws://example.test/ws"
                )

        assertEquals(ChatRuntimeMode.REMOTE, mode)
    }

    @Test
    fun `runtime mode keeps legacy local llm installs local`() {
        val prefs = InMemorySharedPreferences()

        val mode =
                ChatRuntimeModeStore.read(
                        prefs = prefs,
                        legacyLocalLlmEnabled = true,
                        legacyRemoteUrl = "ws://example.test/ws"
                )

        assertEquals(ChatRuntimeMode.LOCAL, mode)
    }

    @Test
    fun `runtime mode explicit value wins over legacy inference`() {
        val prefs =
                InMemorySharedPreferences(
                        ChatRuntimeModeStore.KEY_RUNTIME_MODE to ChatRuntimeMode.LOCAL.wireValue
                )

        val mode =
                ChatRuntimeModeStore.read(
                        prefs = prefs,
                        legacyLocalLlmEnabled = false,
                        legacyRemoteUrl = "ws://example.test/ws"
                )

        assertEquals(ChatRuntimeMode.LOCAL, mode)
    }

    @Test
    fun `runtime mode persist stores wire value`() {
        val prefs = InMemorySharedPreferences()

        ChatRuntimeModeStore.persist(prefs, ChatRuntimeMode.REMOTE)

        assertEquals(
                ChatRuntimeMode.REMOTE.wireValue,
                prefs.getString(ChatRuntimeModeStore.KEY_RUNTIME_MODE, null)
        )
    }
}

private class InMemorySecretStringStore(vararg entries: Pair<String, String>) : SecretStringStore {
    val values = entries.toMap().toMutableMap()

    override fun getString(key: String): String? = values[key]

    override fun putString(key: String, value: String?): Boolean {
        if (value.isNullOrBlank()) values.remove(key) else values[key] = value
        return true
    }
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
