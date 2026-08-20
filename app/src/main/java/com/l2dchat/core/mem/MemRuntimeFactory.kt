package com.l2dchat.core.mem

import androidx.room.withTransaction
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.MemSettings
import com.l2dchat.core.storage.ChatDatabase
import com.l2dchat.core.tools.Tool
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/**
 * Dedicated factory that assembles the mem subsystem's runtime objects and
 * exposes the host planner's ltm tool surface ([buildLtmTools]) as app [Tool]s.
 *
 * Kept separate from [com.l2dchat.core.LocalRuntimeFactory] so that file stays
 * a slim assembly point: every mem-specific construction (config resolution,
 * store + indexes, embedding client, retrieval facade, extraction pipeline,
 * `store.load()` startup) lives here. [com.l2dchat.core.LocalRuntimeFactory]
 * just calls [createLtmTools] and forwards the result into
 * `LocalToolRegistryFactory.normalTools(extraTools = ...)`.
 *
 * ## Conditional registration
 *
 * [createLtmTools] returns an empty list whenever mem is disabled — i.e. when
 * [MemConfigResolver.resolve] returns `null` (settings disabled, or no base
 * URL/model resolvable). This is the "mem disabled → zero behavior change"
 * guarantee: no mem object is constructed, no DB load runs, no tool is
 * registered. The caller does not need its own `if (memSettings.enabled)`
 * guard.
 *
 * ## `store.load()` timing
 *
 * [MemStore.load] is suspend and reads every stored vector + entity card into
 * the in-memory indexes. It must complete before the first ltm tool call (the
 * vector/BM25 indexes would otherwise be empty and every search/expand would
 * miss). The factory launches it on the provided [CoroutineScope] as a
 * fire-and-forget [Job]; the job is exposed via [MemRuntime.lastLoadJob] so the
 * caller (or a test) can await it. In production the first user message always
 * arrives after startup, so the load has time to finish; a tool call that
 * races the load simply sees an empty index until it completes (no crash, no
 * corruption — the indexes are read-safe while being populated).
 *
 * ## `enableStore` decision
 *
 * [MemSettings] has no explicit `enableStore` field. Per the phase-6b task
 * spec ("if no such setting exists, default enableStore=true when pipeline
 * available"), `ltm_store` is registered whenever the extraction pipeline is
 * built — i.e. whenever mem is enabled. The decision is documented here so
 * future work can flip it to a settings-driven flag without touching the
 * factory's callers.
 *
 * @param scope Coroutine scope for [MemStore.load] and as the parent of any
 *   future mem background work. The factory does NOT create its own scope by
 *   default so the caller controls the lifecycle (the runtime's scope is the
 *   natural parent — when the runtime is rebuilt, the load job is cancelled
 *   with it).
 * @param memSettings User-facing mem settings (enabled flag, provider/model
 *   overrides).
 * @param localLlmSettings Host chat-LLM settings — used as a fallback for
 *   base URL / API key when [MemSettings] leaves them unset (see
 *   [MemConfigResolver]).
 * @param database The app's [ChatDatabase]; the four mem DAOs are read from
 *   it. The factory does not open or close the database.
 * @param httpClient Shared [OkHttpClient] for the extraction + embedding
 *   clients. Defaults to a fresh client; the runtime should inject the same
 *   client the rest of the app uses so connection pools stay bounded.
 * @param gson Shared [Gson] for tool payload serialization.
 * @param nowFn Clock for the ltm_store idempotency key and the retrieval rank
 *   decay math. Returns epoch seconds as [Long].
 * @return A [MemRuntime] holding the assembled tools (empty when mem is
 *   disabled) and the [MemRuntime.lastLoadJob] handle.
 */
object MemRuntimeFactory {

    fun createLtmTools(
        scope: CoroutineScope,
        memSettings: MemSettings,
        localLlmSettings: LocalLlmSettings,
        database: ChatDatabase,
        httpClient: OkHttpClient = OkHttpClient.Builder().build(),
        gson: Gson = Gson(),
        nowFn: () -> Long = { System.currentTimeMillis() / 1000L },
    ): MemRuntime {
        val config = MemConfigResolver.resolve(memSettings, localLlmSettings)
            ?: return MemRuntime(
                tools = emptyList(),
                lastLoadJob = null,
                pipeline = null,
                store = null,
                retrieval = null,
            )

        val embeddingModel = config.embeddingModel()
        val embeddingProvider = config.providerFor(embeddingModel)
        val embeddingDim = embeddingModel.dimensions
            ?: memSettings.embeddingDimensions
            ?: DEFAULT_EMBEDDING_DIMENSIONS

        val store = MemStore(
            nodeDao = database.memNodeDao(),
            linkDao = database.memLinkDao(),
            vectorDao = database.memVectorDao(),
            receiptDao = database.memReceiptDao(),
            expectedDim = embeddingDim,
            transactionRunner = { block -> database.withTransaction(block) },
        )

        // store.load() is suspend; launch it on the runtime scope so it runs
        // concurrently with the rest of startup. The first ltm tool call will
        // see a fully-populated index by then; a call that races it sees an
        // empty index (safe — see class doc).
        val loadJob = scope.launch(Dispatchers.IO) { store.load() }

        val embedder: EmbeddingClient = OpenAiCompatibleEmbeddingClient(
            baseUrl = embeddingProvider.baseUrl,
            modelIdentifier = embeddingModel.modelIdentifier,
            dimension = embeddingDim,
            apiKeyProvider = { embeddingProvider.apiKey },
            httpClient = httpClient,
            gson = gson,
        )

        val retrieval = MemRetrieval(
            store = store,
            embedder = embedder,
            nowFn = nowFn,
        )

        val memLlmClient = MemLlmClient(
            config = config,
            httpClient = httpClient,
            gson = gson,
        )
        val pipeline = ExtractionPipeline(
            store = store,
            llm = memLlmClient.extractionClient(multimodal = false),
            config = memLlmClient.extractionConfig(multimodal = false),
            embedder = embedder,
            registry = NodeTypeRegistry(),
            llmMm = memLlmClient.extractionClient(multimodal = true),
            configMm = memLlmClient.extractionConfig(multimodal = true),
        )

        // The search sub agent (design 5.4) reuses the extraction chat client:
        // a mid-size model suffices, and keeping a single provider/model
        // selection avoids a separate config surface. Mirrors Python's
        // `MemSearchAgent(llm=chat_client)` wiring.
        val searchAgent = MemSearchAgent(
            store = store,
            embedder = embedder,
            llm = memLlmClient.extractionClient(multimodal = false),
            config = memLlmClient.extractionConfig(multimodal = false),
            nowFn = nowFn,
        )

        // enableStore defaults to true when the pipeline is available — see
        // class doc. No MemSettings flag exists for it yet.
        val ltmTools = buildLtmTools(
            store = store,
            retrieval = retrieval,
            pipeline = pipeline,
            searchAgent = searchAgent,
            enableStore = true,
            nowFn = nowFn,
        )

        // The mem payload contract serializes explicit nulls (e.g. "error": null
        // means "no error"); the adapter gson must serializeNulls regardless of
        // what the host's shared gson does.
        val adapterGson = GsonBuilder().serializeNulls().create()
        val tools: List<Tool> = ltmTools.values.map { LtmToolAdapter(it, adapterGson) }
        return MemRuntime(
            tools = tools,
            lastLoadJob = loadJob,
            pipeline = pipeline,
            store = store,
            retrieval = retrieval,
        )
    }

    private const val DEFAULT_EMBEDDING_DIMENSIONS: Int = 1024
}

/**
 * Result of [MemRuntimeFactory.createLtmTools].
 *
 * @property tools The adapted ltm tools (empty when mem is disabled). Pass
 *   directly to `LocalToolRegistryFactory.normalTools(extraTools = tools)`.
 * @property lastLoadJob The [MemStore.load] background job, or `null` when mem
 *   is disabled (no load was launched). Tests can await this to observe a
 *   deterministic "indexes ready" point; production code rarely needs it.
 * @property pipeline The assembled [ExtractionPipeline], or `null` when mem is
 *   disabled. The host uses it to ingest conversation compacts and life_capture
 *   batches out-of-band (the ltm tool surface is a separate, on-demand path).
 *   All host uses must be null-safe `?.` — the reference is rebuilt whenever
 *   the runtime is rebuilt (e.g. on a settings change), so a captured reference
 *   can go stale across rebuilds.
 * @property store The assembled [MemStore], or `null` when mem is disabled.
 *   Exposed so the host can read media bytes for an `ltm_read_media` result
 *   without going through the tool surface again.
 * @property retrieval The assembled [MemRetrieval], or `null` when mem is
 *   disabled. Exposed for host-side ranking/lookup symmetry with the tool path.
 */
data class MemRuntime(
    val tools: List<Tool>,
    val lastLoadJob: Job?,
    val pipeline: ExtractionPipeline? = null,
    val store: MemStore? = null,
    val retrieval: MemRetrieval? = null,
)
