package com.l2dchat.core.config

import com.l2dchat.core.LocalRuntimeLlmConfig
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.OpenAiCompatibleClient
import okhttp3.OkHttpClient

data class LocalLlmSettings(
        val enabled: Boolean = false,
        val baseUrl: String? = null,
        val apiKey: String? = null,
        val plannerModel: String? = null,
        val replierModel: String? = null,
        val nativeToolCalling: Boolean = true,
        val temperature: Double? = null,
        val maxTokens: Int? = null,
        val timeoutMillis: Long? = DEFAULT_TIMEOUT_MILLIS
) {
    fun toRuntimeConfig(
            httpClient: OkHttpClient = OkHttpClient.Builder().build()
    ): LocalRuntimeLlmConfig? {
        if (!enabled) return null
        val resolvedBaseUrl = baseUrl.trimmedOrNull() ?: return null
        val resolvedPlannerModel = plannerModel.trimmedOrNull() ?: return null
        val resolvedReplierModel = replierModel.trimmedOrNull() ?: resolvedPlannerModel
        val client =
                OpenAiCompatibleClient(
                        baseUrl = resolvedBaseUrl,
                        apiKeyProvider = { apiKey.trimmedOrNull() },
                        httpClient = httpClient
                )
        return LocalRuntimeLlmConfig(
                plannerClient = client,
                plannerConfig = generationConfig(resolvedPlannerModel),
                replierClient = client,
                replierConfig = generationConfig(resolvedReplierModel),
                nativeToolCalling = nativeToolCalling
        )
    }

    private fun generationConfig(model: String): LlmGenerationConfig =
            LlmGenerationConfig(
                    model = model,
                    temperature = temperature,
                    maxTokens = maxTokens,
                    timeoutMillis = timeoutMillis
            )

    override fun toString(): String =
            "LocalLlmSettings(" +
                    "enabled=$enabled, " +
                    "baseUrl=$baseUrl, " +
                    "apiKey=${apiKey.redactedForLog()}, " +
                    "plannerModel=$plannerModel, " +
                    "replierModel=$replierModel, " +
                    "nativeToolCalling=$nativeToolCalling, " +
                    "temperature=$temperature, " +
                    "maxTokens=$maxTokens, " +
                    "timeoutMillis=$timeoutMillis" +
                    ")"

    companion object {
        const val DEFAULT_TIMEOUT_MILLIS: Long = 60_000L
    }
}

private fun String?.trimmedOrNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }

private fun String?.redactedForLog(): String =
        if (this.isNullOrBlank()) "null" else "<redacted>"
