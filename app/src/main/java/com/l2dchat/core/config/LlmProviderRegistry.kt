package com.l2dchat.core.config

import java.util.Locale

data class LlmProviderDefinition(
        val id: String,
        val displayName: String,
        val baseUrl: String
)

object LlmProviderRegistry {
    private val definitions =
            listOf(
                    LlmProviderDefinition(
                            id = "openai",
                            displayName = "OpenAI",
                            baseUrl = "https://api.openai.com/v1"
                    )
            )

    private val byKey = definitions.associateBy { it.id.providerKey() }

    fun all(): List<LlmProviderDefinition> = definitions

    fun resolve(provider: String?): LlmProviderDefinition? {
        val key = provider.providerKey().takeIf { it.isNotEmpty() } ?: return null
        return byKey[key]
    }

    fun resolveBaseUrl(provider: String?): String? = resolve(provider)?.baseUrl
}

private fun String?.providerKey(): String =
        this?.trim()
                ?.lowercase(Locale.ROOT)
                ?.replace(Regex("[\\s_-]+"), "")
                .orEmpty()
