package com.l2dchat.core.tools

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

enum class ReplierTaskState {
    PENDING,
    GENERATING,
    BACKGROUND,
    COMPLETED,
    CANCELLED,
    FAILED
}

data class ReplierTaskRequest(
        val taskId: String,
        val routingKey: RoutingKey,
        val trigger: Trigger,
        val content: String,
        val replyGuidance: String? = null,
        val styleOverride: String? = null,
        val emotionHint: String? = null,
        val isProgressUpdate: Boolean = false,
        val includeAction: Boolean = false,
        val liveImage: String? = null
) {
    init {
        require(taskId.isNotBlank()) { "Replier task id must not be blank" }
        require(content.isNotBlank()) { "Replier task content must not be blank" }
        require(replyGuidance == null || replyGuidance.isNotBlank()) {
            "Replier task replyGuidance must not be blank"
        }
        require(styleOverride == null || styleOverride.isNotBlank()) {
            "Replier task styleOverride must not be blank"
        }
        require(emotionHint == null || emotionHint.isNotBlank()) {
            "Replier task emotionHint must not be blank"
        }
        require(liveImage == null || liveImage.isNotBlank()) {
            "Replier task liveImage must not be blank"
        }
    }
}

data class ReplierTaskSnapshot(
        val taskId: String,
        val state: ReplierTaskState,
        val previewText: String = "",
        val replyText: String? = null,
        val errorMessage: String? = null,
        val backgrounded: Boolean = false
) {
    val isTerminal: Boolean
        get() =
                state == ReplierTaskState.COMPLETED ||
                        state == ReplierTaskState.CANCELLED ||
                        state == ReplierTaskState.FAILED
}

data class ReplierTaskSummary(
        val taskId: String,
        val routingKey: RoutingKey,
        val triggerMessageId: String,
        val triggerText: String,
        val state: ReplierTaskState,
        val previewText: String,
        val replyText: String?,
        val errorMessage: String?,
        val backgrounded: Boolean
)

sealed interface ReplierTaskUpdate {
    data class TextDelta(val text: String) : ReplierTaskUpdate

    data class Preview(val text: String) : ReplierTaskUpdate

    data class Completed(val replyText: String) : ReplierTaskUpdate
}

fun interface ReplierTaskGenerator {
    fun generate(request: ReplierTaskRequest): Flow<ReplierTaskUpdate>
}

class ReplierTask(
        val request: ReplierTaskRequest,
        private val scope: CoroutineScope,
        private val generator: ReplierTaskGenerator
) {
    private val lock = Any()
    private val mutableSnapshot =
            MutableStateFlow(
                    ReplierTaskSnapshot(
                            taskId = request.taskId,
                            state = ReplierTaskState.PENDING
                    )
            )
    private var job: Job? = null

    val snapshots: StateFlow<ReplierTaskSnapshot> = mutableSnapshot.asStateFlow()

    val snapshot: ReplierTaskSnapshot
        get() = mutableSnapshot.value

    fun start(): ReplierTask =
            synchronized(lock) {
                if (job != null) {
                    return@synchronized this
                }
                mutableSnapshot.value =
                        mutableSnapshot.value.copy(state = ReplierTaskState.GENERATING)
                job = scope.launch { runGeneration() }
                this
            }

    fun moveToBackground(): Boolean =
            synchronized(lock) {
                val current = mutableSnapshot.value
                if (current.state != ReplierTaskState.GENERATING) {
                    return@synchronized false
                }
                mutableSnapshot.value =
                        current.copy(state = ReplierTaskState.BACKGROUND, backgrounded = true)
                true
            }

    suspend fun waitForCompletion(): ReplierTaskSnapshot =
            snapshots.first { it.isTerminal }

    suspend fun cancel(): ReplierTaskSnapshot {
        val currentJob = synchronized(lock) { job }
        if (currentJob == null) {
            mutableSnapshot.value =
                    mutableSnapshot.value.copy(state = ReplierTaskState.CANCELLED)
            return mutableSnapshot.value
        }
        currentJob.cancelAndJoin()
        if (!mutableSnapshot.value.isTerminal) {
            mutableSnapshot.value =
                    mutableSnapshot.value.copy(state = ReplierTaskState.CANCELLED)
        }
        return mutableSnapshot.value
    }

    private suspend fun runGeneration() {
        val draft = StringBuilder()
        var completedText: String? = null
        try {
            generator.generate(request).collect { update ->
                when (update) {
                    is ReplierTaskUpdate.TextDelta -> {
                        if (update.text.isNotEmpty()) {
                            draft.append(update.text)
                            updatePreview(draft.toString())
                        }
                    }
                    is ReplierTaskUpdate.Preview -> updatePreview(update.text)
                    is ReplierTaskUpdate.Completed -> {
                        completedText = update.replyText
                        updatePreview(update.replyText)
                    }
                }
            }
            val replyText = completedText ?: draft.toString()
            if (replyText.isBlank()) {
                fail("Replier task completed without reply text")
                return
            }
            mutableSnapshot.value =
                    mutableSnapshot.value.copy(
                            state = ReplierTaskState.COMPLETED,
                            previewText = replyText,
                            replyText = replyText
                    )
        } catch (error: CancellationException) {
            mutableSnapshot.value =
                    mutableSnapshot.value.copy(state = ReplierTaskState.CANCELLED)
            throw error
        } catch (error: Throwable) {
            fail(error.message ?: error::class.java.simpleName)
        }
    }

    private fun updatePreview(previewText: String) {
        val current = mutableSnapshot.value
        if (current.isTerminal) {
            return
        }
        val state =
                if (current.state == ReplierTaskState.BACKGROUND) {
                    ReplierTaskState.BACKGROUND
                } else {
                    ReplierTaskState.GENERATING
                }
        mutableSnapshot.value = current.copy(state = state, previewText = previewText)
    }

    private fun fail(message: String) {
        mutableSnapshot.value =
                mutableSnapshot.value.copy(
                        state = ReplierTaskState.FAILED,
                        errorMessage = message
                )
    }
}
