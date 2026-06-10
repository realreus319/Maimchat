package com.l2dchat.core.config

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.l2dchat.core.LocalRuntimeLlmConfig
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.OpenAiCompatibleClient
import com.l2dchat.core.message.AgentConfigEntity
import okhttp3.OkHttpClient

data class AgentLlmSettingsOverride(
        val baseUrl: String? = null,
        val plannerModel: String? = null,
        val replierModel: String? = null,
        val nativeToolCalling: Boolean? = null,
        val temperature: Double? = null,
        val maxTokens: Int? = null,
        val timeoutMillis: Long? = null
) {
    companion object {
        fun fromAgentConfig(config: AgentConfigEntity?): AgentLlmSettingsOverride? =
                config?.toLocalLlmSettingsOverride()
    }
}

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
            httpClient: OkHttpClient = OkHttpClient.Builder().build(),
            agentOverride: AgentLlmSettingsOverride? = null
    ): LocalRuntimeLlmConfig? {
        if (!enabled) return null
        val resolvedBaseUrl = agentOverride?.baseUrl.trimmedOrNull() ?: baseUrl.trimmedOrNull()
                ?: return null
        val resolvedPlannerModel =
                agentOverride?.plannerModel.trimmedOrNull() ?: plannerModel.trimmedOrNull()
                        ?: return null
        val resolvedReplierModel =
                agentOverride?.replierModel.trimmedOrNull()
                        ?: replierModel.trimmedOrNull()
                        ?: resolvedPlannerModel
        val client =
                OpenAiCompatibleClient(
                        baseUrl = resolvedBaseUrl,
                        apiKeyProvider = { apiKey.trimmedOrNull() },
                        httpClient = httpClient
                )
        return LocalRuntimeLlmConfig(
                plannerClient = client,
                plannerConfig = generationConfig(resolvedPlannerModel, agentOverride),
                replierClient = client,
                replierConfig = generationConfig(resolvedReplierModel, agentOverride),
                nativeToolCalling = agentOverride?.nativeToolCalling ?: nativeToolCalling
        )
    }

    private fun generationConfig(
            model: String,
            agentOverride: AgentLlmSettingsOverride?
    ): LlmGenerationConfig =
            LlmGenerationConfig(
                    model = model,
                    temperature = agentOverride?.temperature ?: temperature,
                    maxTokens = agentOverride?.maxTokens ?: maxTokens,
                    timeoutMillis = agentOverride?.timeoutMillis ?: timeoutMillis
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

private fun AgentConfigEntity.toLocalLlmSettingsOverride(): AgentLlmSettingsOverride? {
    val settings = settingsJson.toSettingsObjectOrNull()
    val profileModel = model.trimmedOrNull()
    val providerLabel = provider.trimmedOrNull()
    val providerBaseUrl =
            providerLabel?.let { label ->
                LlmProviderRegistry.resolveBaseUrl(label) ?: label.takeIf { it.isHttpUrlLike() }
            }
    return AgentLlmSettingsOverride(
                    baseUrl =
                            settings?.stringOrNull("base_url", "baseUrl")
                                    ?: providerBaseUrl,
                    plannerModel =
                            settings?.stringOrNull("planner_model", "plannerModel")
                                    ?: profileModel,
                    replierModel =
                            settings?.stringOrNull("replier_model", "replierModel")
                                    ?: profileModel,
                    nativeToolCalling =
                            settings?.booleanOrNull(
                                    "native_tool_calling",
                                    "nativeToolCalling"
                            ),
                    temperature =
                            settings
                                    ?.doubleOrNull("temperature")
                                    ?.takeIf { it in 0.0..2.0 },
                    maxTokens =
                            settings
                                    ?.intOrNull("max_tokens", "maxTokens")
                                    ?.takeIf { it > 0 },
                    timeoutMillis =
                            settings
                                    ?.longOrNull("timeout_millis", "timeoutMillis")
                                    ?.takeIf { it > 0L }
            )
            .takeIf { it.hasAnyValue() }
}

private fun String?.trimmedOrNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }

private fun String?.redactedForLog(): String =
        if (this.isNullOrBlank()) "null" else "<redacted>"

private fun AgentLlmSettingsOverride.hasAnyValue(): Boolean =
        listOf(
                        baseUrl,
                        plannerModel,
                        replierModel,
                        nativeToolCalling,
                        temperature,
                        maxTokens,
                        timeoutMillis
                )
                .any { it != null }

private fun String.isHttpUrlLike(): Boolean =
        startsWith("https://", ignoreCase = true) || startsWith("http://", ignoreCase = true)

private fun String?.toSettingsObjectOrNull(): JsonObject? {
    val text = trimmedOrNull() ?: return null
    return runCatching { JsonParser.parseString(text) }
            .getOrNull()
            ?.takeIf { it.isJsonObject }
            ?.asJsonObject
}

private fun JsonObject.stringOrNull(vararg names: String): String? {
    names.forEach { name ->
        val element = get(name) ?: return@forEach
        if (!element.isJsonNull && element.isJsonPrimitive) {
            val primitive = element.asJsonPrimitive
            if (primitive.isString) {
                primitive.asString.trimmedOrNull()?.let { return it }
            }
        }
    }
    return null
}

private fun JsonObject.booleanOrNull(vararg names: String): Boolean? {
    names.forEach { name ->
        val element = get(name) ?: return@forEach
        if (!element.isJsonNull && element.isJsonPrimitive) {
            val primitive = element.asJsonPrimitive
            when {
                primitive.isBoolean -> return primitive.asBoolean
                primitive.isString ->
                        when (primitive.asString.trim().lowercase()) {
                            "true" -> return true
                            "false" -> return false
                        }
            }
        }
    }
    return null
}

private fun JsonObject.doubleOrNull(vararg names: String): Double? {
    names.forEach { name ->
        val primitive = primitiveOrNull(name) ?: return@forEach
        val parsed =
                when {
                    primitive.isNumber -> runCatching { primitive.asDouble }.getOrNull()
                    primitive.isString -> primitive.asString.trim().toDoubleOrNull()
                    else -> null
                }
        parsed?.let { return it }
    }
    return null
}

private fun JsonObject.intOrNull(vararg names: String): Int? {
    names.forEach { name ->
        val primitive = primitiveOrNull(name) ?: return@forEach
        val parsed =
                when {
                    primitive.isNumber -> runCatching { primitive.asInt }.getOrNull()
                    primitive.isString -> primitive.asString.trim().toIntOrNull()
                    else -> null
                }
        parsed?.let { return it }
    }
    return null
}

private fun JsonObject.longOrNull(vararg names: String): Long? {
    names.forEach { name ->
        val primitive = primitiveOrNull(name) ?: return@forEach
        val parsed =
                when {
                    primitive.isNumber -> runCatching { primitive.asLong }.getOrNull()
                    primitive.isString -> primitive.asString.trim().toLongOrNull()
                    else -> null
                }
        parsed?.let { return it }
    }
    return null
}

private fun JsonObject.primitiveOrNull(name: String) =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asJsonPrimitive
