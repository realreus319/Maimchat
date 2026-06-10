package com.l2dchat.chat.service

import android.content.SharedPreferences
import com.l2dchat.core.config.LocalLlmSettings

internal object LocalLlmSettingsStore {
    const val KEY_LOCAL_LLM_ENABLED = "local_llm_enabled"
    const val KEY_LOCAL_LLM_BASE_URL = "local_llm_base_url"
    const val KEY_LOCAL_LLM_API_KEY = ChatSecurePreferences.KEY_LOCAL_LLM_API_KEY
    const val KEY_LOCAL_LLM_PLANNER_MODEL = "local_llm_planner_model"
    const val KEY_LOCAL_LLM_REPLIER_MODEL = "local_llm_replier_model"
    const val KEY_LOCAL_LLM_NATIVE_TOOL_CALLING = "local_llm_native_tool_calling"
    const val KEY_LOCAL_LLM_TEMPERATURE = "local_llm_temperature"
    const val KEY_LOCAL_LLM_MAX_TOKENS = "local_llm_max_tokens"
    const val KEY_LOCAL_LLM_TIMEOUT_MILLIS = "local_llm_timeout_millis"

    fun read(prefs: SharedPreferences, secureStore: SecretStringStore): LocalLlmSettings =
            LocalLlmSettings(
                    enabled = prefs.getBoolean(KEY_LOCAL_LLM_ENABLED, false),
                    baseUrl = prefs.getString(KEY_LOCAL_LLM_BASE_URL, null),
                    apiKey =
                            ChatSecurePreferences.readMigratingString(
                                    prefs,
                                    secureStore,
                                    KEY_LOCAL_LLM_API_KEY
                            ),
                    plannerModel = prefs.getString(KEY_LOCAL_LLM_PLANNER_MODEL, null),
                    replierModel = prefs.getString(KEY_LOCAL_LLM_REPLIER_MODEL, null),
                    nativeToolCalling =
                            prefs.getBoolean(KEY_LOCAL_LLM_NATIVE_TOOL_CALLING, true),
                    temperature =
                            prefs.getString(KEY_LOCAL_LLM_TEMPERATURE, null)?.toDoubleOrNull(),
                    maxTokens =
                            if (prefs.contains(KEY_LOCAL_LLM_MAX_TOKENS)) {
                                prefs.getInt(KEY_LOCAL_LLM_MAX_TOKENS, 0).takeIf { it > 0 }
                            } else {
                                null
                            },
                    timeoutMillis =
                            if (prefs.contains(KEY_LOCAL_LLM_TIMEOUT_MILLIS)) {
                                prefs.getLong(KEY_LOCAL_LLM_TIMEOUT_MILLIS, 0L)
                                        .takeIf { it > 0L }
                            } else {
                                LocalLlmSettings.DEFAULT_TIMEOUT_MILLIS
                            }
            )

    fun persist(
            prefs: SharedPreferences,
            secureStore: SecretStringStore,
            settings: LocalLlmSettings
    ) {
        ChatSecurePreferences.writeString(secureStore, KEY_LOCAL_LLM_API_KEY, settings.apiKey)
        prefs.edit()
                .putBoolean(KEY_LOCAL_LLM_ENABLED, settings.enabled)
                .putBoolean(KEY_LOCAL_LLM_NATIVE_TOOL_CALLING, settings.nativeToolCalling)
                .putOptionalString(KEY_LOCAL_LLM_BASE_URL, settings.baseUrl)
                .remove(KEY_LOCAL_LLM_API_KEY)
                .putOptionalString(KEY_LOCAL_LLM_PLANNER_MODEL, settings.plannerModel)
                .putOptionalString(KEY_LOCAL_LLM_REPLIER_MODEL, settings.replierModel)
                .putOptionalDouble(KEY_LOCAL_LLM_TEMPERATURE, settings.temperature)
                .putOptionalInt(KEY_LOCAL_LLM_MAX_TOKENS, settings.maxTokens)
                .putOptionalLong(KEY_LOCAL_LLM_TIMEOUT_MILLIS, settings.timeoutMillis)
                .apply()
    }
}

private fun SharedPreferences.Editor.putOptionalString(
        key: String,
        value: String?
): SharedPreferences.Editor = if (value.isNullOrBlank()) remove(key) else putString(key, value)

private fun SharedPreferences.Editor.putOptionalDouble(
        key: String,
        value: Double?
): SharedPreferences.Editor = if (value == null) remove(key) else putString(key, value.toString())

private fun SharedPreferences.Editor.putOptionalInt(
        key: String,
        value: Int?
): SharedPreferences.Editor = if (value == null) remove(key) else putInt(key, value)

private fun SharedPreferences.Editor.putOptionalLong(
        key: String,
        value: Long?
): SharedPreferences.Editor = if (value == null) remove(key) else putLong(key, value)
