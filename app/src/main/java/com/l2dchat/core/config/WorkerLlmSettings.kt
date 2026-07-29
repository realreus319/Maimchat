package com.l2dchat.core.config

import com.l2dchat.BuildConfig
import org.json.JSONObject

/**
 * Configuration for the on-device worker (the cc_research Claude-Code port, run by the engine package
 * under proot). The user enters the basics — [baseUrl] / [apiKey] / [model] — and [settingsJson] is the
 * editable Claude-Code-format `settings.json` that is shipped to the worker.
 *
 * The three basics drive the worker's connection (exported as `OPENAI_*` env vars by the engine).
 * [settingsJson] is the full CC config (model + env + mcpServers + agent + …); it is written to the
 * worker's `/opt/cfg/settings.json`. When it is blank or does NOT parse as JSON, the model-only baseline
 * built from the three basics is used instead — so the worker is never handed broken config.
 */
data class WorkerLlmSettings(
        val baseUrl: String? = null,
        val apiKey: String? = null,
        val model: String? = null,
        val settingsJson: String? = null,
) {
    /** True once enough is set to reach a provider. */
    fun hasConnection(): Boolean = !baseUrl.isNullOrBlank() && !model.isNullOrBlank()

    /** The CC-format `settings.json` actually shipped: the user's edited JSON if it parses, else the
     *  model-only baseline rebuilt from the three basics. */
    fun effectiveSettingsJson(): String {
        val custom = settingsJson?.takeIf { it.isNotBlank() && isValidJsonObject(it) }
        return custom ?: buildModelOnlyJson(baseUrl, apiKey, model)
    }

    companion object {
        // Provider (base_url + api_key) comes from local.properties (debug only, gitignored). The
        // worker MODEL is committed (baked in defaultConfig): kimi-k2.7-code by default. Used as the
        // fallback when no worker config is stored.
        val DEFAULT_BASE_URL: String? = BuildConfig.LLM_DEFAULT_BASE_URL.ifBlank { null }
        val DEFAULT_API_KEY: String? = BuildConfig.LLM_DEFAULT_API_KEY.ifBlank { null }
        val DEFAULT_MODEL: String? = BuildConfig.LLM_DEFAULT_WORKER_MODEL.ifBlank { null }

        /** True if [text] parses as a JSON object (the shape `settings.json` must be). */
        fun isValidJsonObject(text: String): Boolean =
                try {
                    JSONObject(text)
                    true
                } catch (_: Throwable) {
                    false
                }

        /**
         * The "仅模型配置" baseline: a CC-format `settings.json` containing ONLY the model/connection
         * config (no mcpServers/agent/hooks). This is what the editor reverts to when the edited JSON
         * is invalid.
         */
        fun buildModelOnlyJson(baseUrl: String?, apiKey: String?, model: String?): String {
            val env =
                    JSONObject()
                            .put("MODEL_PROVIDER", "openai")
                            .put("OPENAI_BASE_URL", baseUrl.orEmpty())
                            .put("OPENAI_API_KEY", apiKey.orEmpty())
                            .put("OPENAI_MODEL", model.orEmpty())
            return JSONObject().put("env", env).put("model", model.orEmpty()).toString(2)
        }
    }
}
