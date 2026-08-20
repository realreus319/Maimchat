package com.l2dchat.core.config

import android.content.SharedPreferences

/**
 * Minimal secret-store contract for mem settings persistence.
 *
 * Defined locally (rather than reusing the chat.service SecretStringStore, which is internal to
 * that package) so [MemSettingsStore] stays standalone and testable in core.config. The runtime
 * adapter wraps the app's encrypted [com.l2dchat.chat.service.SecurePreferenceStore].
 */
interface MemSecretStore {
    fun getString(key: String): String?

    fun putString(key: String, value: String?): Boolean
}

/**
 * Persistence for [MemSettings]: regular SharedPreferences for non-secret fields, [MemSecretStore]
 * for the two api keys. Mirrors the LocalLlmSettingsStore / WorkerLlmSettingsStore pattern.
 *
 * Keys are `mem_`-prefixed to avoid collisions. The api keys are stored under
 * [KEY_MEM_EXTRACTION_API_KEY] / [KEY_MEM_EMBEDDING_API_KEY] in the secret store; on [persist]
 * successfully written legacy plaintext copies are removed from the regular prefs, and on [read]
 * a one-time migration moves a legacy plaintext value into the secret store (same shape as
 * ChatSecurePreferences.readMigratingString). A failed secure write keeps the legacy value so the
 * next read can retry without losing the key.
 */
object MemSettingsStore {
    const val KEY_MEM_ENABLED = "mem_enabled"
    const val KEY_MEM_EXTRACTION_BASE_URL = "mem_extraction_base_url"
    const val KEY_MEM_EXTRACTION_API_KEY = "mem_extraction_api_key"
    const val KEY_MEM_EXTRACTION_MODEL = "mem_extraction_model"
    const val KEY_MEM_EXTRACTION_MM_MODEL = "mem_extraction_mm_model"
    const val KEY_MEM_EMBEDDING_BASE_URL = "mem_embedding_base_url"
    const val KEY_MEM_EMBEDDING_API_KEY = "mem_embedding_api_key"
    const val KEY_MEM_EMBEDDING_MODEL = "mem_embedding_model"
    const val KEY_MEM_EMBEDDING_DIMENSIONS = "mem_embedding_dimensions"

    fun read(prefs: SharedPreferences, secretStore: MemSecretStore): MemSettings =
            MemSettings(
                    enabled = prefs.getBoolean(KEY_MEM_ENABLED, false),
                    extractionBaseUrl = prefs.getString(KEY_MEM_EXTRACTION_BASE_URL, null),
                    extractionApiKey =
                            readMigratingString(prefs, secretStore, KEY_MEM_EXTRACTION_API_KEY),
                    extractionModel =
                            prefs.getString(KEY_MEM_EXTRACTION_MODEL, null)
                                    ?: MemSettings.DEFAULT_EXTRACTION_MODEL,
                    extractionMmModel =
                            prefs.getString(KEY_MEM_EXTRACTION_MM_MODEL, null)
                                    ?: MemSettings.DEFAULT_EXTRACTION_MM_MODEL,
                    embeddingBaseUrl =
                            prefs.getString(KEY_MEM_EMBEDDING_BASE_URL, null)
                                    ?: MemSettings.DEFAULT_EMBEDDING_BASE_URL,
                    embeddingApiKey =
                            readMigratingString(prefs, secretStore, KEY_MEM_EMBEDDING_API_KEY),
                    embeddingModel =
                            prefs.getString(KEY_MEM_EMBEDDING_MODEL, null)
                                    ?: MemSettings.DEFAULT_EMBEDDING_MODEL,
                    embeddingDimensions =
                            if (prefs.contains(KEY_MEM_EMBEDDING_DIMENSIONS)) {
                                prefs.getInt(KEY_MEM_EMBEDDING_DIMENSIONS, 0).takeIf { it > 0 }
                                        ?: MemSettings.DEFAULT_EMBEDDING_DIMENSIONS
                            } else {
                                MemSettings.DEFAULT_EMBEDDING_DIMENSIONS
                            }
            )

    fun persist(
            prefs: SharedPreferences,
            secretStore: MemSecretStore,
            settings: MemSettings
    ): Boolean {
        val extractionStored =
                writeSecret(secretStore, KEY_MEM_EXTRACTION_API_KEY, settings.extractionApiKey)
        val embeddingStored =
                writeSecret(secretStore, KEY_MEM_EMBEDDING_API_KEY, settings.embeddingApiKey)
        val editor =
                prefs.edit()
                        .putBoolean(KEY_MEM_ENABLED, settings.enabled)
                .putOptionalString(KEY_MEM_EXTRACTION_BASE_URL, settings.extractionBaseUrl)
                .putOptionalString(KEY_MEM_EXTRACTION_MODEL, settings.extractionModel)
                .putOptionalString(KEY_MEM_EXTRACTION_MM_MODEL, settings.extractionMmModel)
                .putOptionalString(KEY_MEM_EMBEDDING_BASE_URL, settings.embeddingBaseUrl)
                .putOptionalString(KEY_MEM_EMBEDDING_MODEL, settings.embeddingModel)
                .putOptionalInt(KEY_MEM_EMBEDDING_DIMENSIONS, settings.embeddingDimensions)
        if (extractionStored) editor.remove(KEY_MEM_EXTRACTION_API_KEY)
        if (embeddingStored) editor.remove(KEY_MEM_EMBEDDING_API_KEY)
        editor.apply()
        return extractionStored && embeddingStored
    }

    private fun readMigratingString(
            prefs: SharedPreferences,
            secretStore: MemSecretStore,
            key: String
    ): String? {
        val secureValue = secretStore.getString(key)?.takeUnless { it.isBlank() }
        if (secureValue != null) {
            prefs.edit().remove(key).apply()
            return secureValue
        }
        val legacyValue = prefs.getString(key, null)?.takeUnless { it.isBlank() }
        if (prefs.contains(key)) {
            val migrated = legacyValue == null || secretStore.putString(key, legacyValue)
            if (migrated) prefs.edit().remove(key).apply()
        }
        return legacyValue
    }

    private fun writeSecret(secretStore: MemSecretStore, key: String, value: String?): Boolean =
            secretStore.putString(key, value?.takeUnless { it.isBlank() })
}

private fun SharedPreferences.Editor.putOptionalString(
        key: String,
        value: String?
): SharedPreferences.Editor = if (value.isNullOrBlank()) remove(key) else putString(key, value)

private fun SharedPreferences.Editor.putOptionalInt(
        key: String,
        value: Int?
): SharedPreferences.Editor = if (value == null) remove(key) else putInt(key, value)
