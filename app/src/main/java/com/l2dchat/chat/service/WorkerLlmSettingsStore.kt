package com.l2dchat.chat.service

import android.content.SharedPreferences
import com.l2dchat.core.config.WorkerLlmSettings

/** Persistence for [WorkerLlmSettings] (chat_prefs + secure store for the api key), mirroring
 *  [LocalLlmSettingsStore]. */
internal object WorkerLlmSettingsStore {
    const val KEY_WORKER_BASE_URL = "worker_llm_base_url"
    const val KEY_WORKER_MODEL = "worker_llm_model"
    const val KEY_WORKER_SETTINGS_JSON = "worker_llm_settings_json"
    const val KEY_WORKER_API_KEY = ChatSecurePreferences.KEY_WORKER_LLM_API_KEY

    fun read(prefs: SharedPreferences, secureStore: SecretStringStore): WorkerLlmSettings =
            WorkerLlmSettings(
                    baseUrl =
                            prefs.getString(KEY_WORKER_BASE_URL, null)
                                    ?: WorkerLlmSettings.DEFAULT_BASE_URL,
                    apiKey =
                            ChatSecurePreferences.readMigratingString(
                                    prefs,
                                    secureStore,
                                    KEY_WORKER_API_KEY
                            )
                                    ?: WorkerLlmSettings.DEFAULT_API_KEY,
                    model =
                            prefs.getString(KEY_WORKER_MODEL, null)
                                    ?: WorkerLlmSettings.DEFAULT_MODEL,
                    settingsJson = prefs.getString(KEY_WORKER_SETTINGS_JSON, null),
            )

    fun persist(
            prefs: SharedPreferences,
            secureStore: SecretStringStore,
            settings: WorkerLlmSettings
    ) {
        ChatSecurePreferences.writeString(secureStore, KEY_WORKER_API_KEY, settings.apiKey)
        prefs.edit()
                .remove(KEY_WORKER_API_KEY)
                .putWorkerOptionalString(KEY_WORKER_BASE_URL, settings.baseUrl)
                .putWorkerOptionalString(KEY_WORKER_MODEL, settings.model)
                .putWorkerOptionalString(KEY_WORKER_SETTINGS_JSON, settings.settingsJson)
                .apply()
    }
}

private fun SharedPreferences.Editor.putWorkerOptionalString(
        key: String,
        value: String?
): SharedPreferences.Editor = if (value.isNullOrBlank()) remove(key) else putString(key, value)
