package com.l2dchat.core.mem

/**
 * Configuration model for the mem library's LLM/embedding access.
 *
 * Mirrors the structure of `config/mem.toml` from the Python reference:
 * `[[providers]]` / `[[models]]` / `[tasks]`. Unlike the Python version, API
 * keys are stored directly on the provider (resolved by the host before
 * constructing this config) instead of indirectly via environment variable
 * names — the host owns secret storage ([com.l2dchat.core.config] layer).
 */

/**
 * A configured LLM/embedding provider endpoint.
 *
 * @property name Unique provider name referenced by models.
 * @property baseUrl OpenAI-compatible endpoint base URL.
 * @property apiKey API key for the endpoint; `null` for key-less providers
 *   (e.g. a local inference service). The host resolves the actual secret
 *   before constructing this object.
 * @property timeoutSeconds Request timeout in seconds.
 */
data class MemProvider(
    val name: String,
    val baseUrl: String,
    val apiKey: String? = null,
    val timeoutSeconds: Int = 120,
) {
    init {
        require(name.isNotBlank()) { "MemProvider: name must be non-blank" }
        require(baseUrl.isNotBlank()) { "MemProvider: baseUrl must be non-blank" }
        require(timeoutSeconds > 0) { "MemProvider: timeoutSeconds must be positive" }
    }
}

/** Whether a [MemModel] is used for chat/completions or for embeddings. */
enum class ModelKind { CHAT, EMBEDDING }

/**
 * A configured model as declared in the config (provider not yet joined).
 *
 * @property name Unique model name referenced by tasks.
 * @property provider Name of the provider serving this model.
 * @property modelIdentifier Provider-side model identifier sent in API calls.
 * @property kind [ModelKind.CHAT] or [ModelKind.EMBEDDING].
 * @property maxTokens Max tokens parameter for chat calls; `null` lets the
 *   provider default apply.
 * @property temperature Temperature parameter for chat calls; `null` lets the
 *   provider default apply.
 * @property dimensions Vector dimension; required when [kind] is
 *   [ModelKind.EMBEDDING], ignored otherwise.
 */
data class MemModel(
    val name: String,
    val provider: String,
    val modelIdentifier: String,
    val kind: ModelKind,
    val maxTokens: Int? = null,
    val temperature: Double? = null,
    val dimensions: Int? = null,
) {
    init {
        require(name.isNotBlank()) { "MemModel: name must be non-blank" }
        require(provider.isNotBlank()) { "MemModel: provider must be non-blank" }
        require(modelIdentifier.isNotBlank()) { "MemModel: modelIdentifier must be non-blank" }
        require(maxTokens == null || maxTokens > 0) {
            "MemModel: maxTokens must be positive when provided"
        }
        require(temperature == null || temperature in 0.0..2.0) {
            "MemModel: temperature must be between 0 and 2"
        }
        require(dimensions == null || dimensions > 0) {
            "MemModel: dimensions must be positive when provided"
        }
    }
}

/**
 * Task → model name mapping.
 *
 * [extraction], [extractionMm] and [embedding] are required. [vision] and
 * [websearch] are pass-through entries the host uses for purpose-specific
 * models outside the mem core (image summarisation, search-result digest);
 * they are not consumed by [MemLlmClient] but are carried so the host can read
 * a single config object.
 */
data class MemTaskConfig(
    val extraction: String,
    val extractionMm: String,
    val embedding: String,
    val vision: String? = null,
    val websearch: String? = null,
) {
    init {
        require(extraction.isNotBlank()) { "MemTaskConfig: extraction must be non-blank" }
        require(extractionMm.isNotBlank()) { "MemTaskConfig: extractionMm must be non-blank" }
        require(embedding.isNotBlank()) { "MemTaskConfig: embedding must be non-blank" }
    }
}

/**
 * Loaded mem configuration: providers, models and the task mapping.
 *
 * Resolution methods throw [ConfigError] on any missing reference so callers
 * get a single, typed failure instead of `null`-chasing.
 */
data class MemLlmConfig(
    val providers: Map<String, MemProvider>,
    val models: Map<String, MemModel>,
    val tasks: MemTaskConfig,
) {
    /** Resolve the chat model used for non-multimodal extraction. */
    fun extractionModel(): MemModel =
        models[tasks.extraction]
            ?: throw ConfigError("extraction model '${tasks.extraction}' not found")

    /** Resolve the chat model used for multimodal extraction. */
    fun extractionMmModel(): MemModel =
        models[tasks.extractionMm]
            ?: throw ConfigError("extraction_mm model '${tasks.extractionMm}' not found")

    /** Resolve the embedding model. */
    fun embeddingModel(): MemModel =
        models[tasks.embedding]
            ?: throw ConfigError("embedding model '${tasks.embedding}' not found")

    /** Resolve the provider backing a model, throwing if the reference is dangling. */
    fun providerFor(model: MemModel): MemProvider =
        providers[model.provider]
            ?: throw ConfigError("provider '${model.provider}' not found (referenced by model '${model.name}')")
}
