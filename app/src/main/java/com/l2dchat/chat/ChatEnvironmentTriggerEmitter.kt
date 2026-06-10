package com.l2dchat.chat

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
import com.l2dchat.core.trigger.TriggerPriority
import java.util.Locale

data class ChatEnvironmentTriggerContext(
        val routingKey: RoutingKey,
        val modelKey: String? = null,
        val modelName: String? = null,
        val userId: String? = null,
        val userName: String? = null,
        val agentUserId: String? = null,
        val agentName: String? = null
)

class ChatEnvironmentTriggerEmitter(
        private val clockMillis: () -> Long = { System.currentTimeMillis() },
        private val minIntervalsMillis: Map<String, Long> = DEFAULT_MIN_INTERVALS_MILLIS
) {
    private val emissionLock = Any()
    private val lastEmissions = linkedMapOf<String, Emission>()

    fun onModelChanged(context: ChatEnvironmentTriggerContext): List<EnvironmentTriggerSubmission> {
        val displayName = context.modelName.cleanOrNull() ?: context.modelKey.cleanOrNull()
                ?: return emptyList()
        val signature = signatureOf(context.modelKey, context.modelName)
        return emit(
                context = context,
                eventType = EVENT_MODEL_CHANGED,
                signature = signature,
                text = "Live2D 模型已切换为：$displayName。",
                source = SOURCE_ANDROID_APP,
                priority = TriggerPriority.NORMAL
        )
    }

    fun onEnvironmentUpdate(
            update: ChatEnvironmentUpdate,
            context: ChatEnvironmentTriggerContext
    ): List<EnvironmentTriggerSubmission> {
        val submissions = mutableListOf<EnvironmentTriggerSubmission>()
        if (update.hasAppVisible && update.appVisible == true) {
            submissions +=
                    emit(
                            context = context,
                            eventType = EVENT_APP_VISIBLE,
                            signature =
                                    signatureOf(
                                            context.modelKey,
                                            context.modelName,
                                            update.lifecycleState
                                    ),
                            text = "应用回到前台，Live2D 画面已可见。",
                            source = SOURCE_ANDROID_APP,
                            priority = TriggerPriority.LOW,
                            metadata = mapOf("lifecycle_state" to update.lifecycleState)
                    )
        }
        if (update.hasWallpaperVisible && update.wallpaperVisible == true) {
            submissions +=
                    emit(
                            context = context,
                            eventType = EVENT_WALLPAPER_VISIBLE,
                            signature = signatureOf(context.modelKey, context.modelName),
                            text = "Live2D 壁纸已可见。",
                            source = SOURCE_ANDROID_WALLPAPER,
                            priority = TriggerPriority.LOW
                    )
        }
        if (update.hasBackgroundPath) {
            val backgroundPath = update.backgroundPath.cleanOrNull()
            submissions +=
                    emit(
                            context = context,
                            eventType = EVENT_BACKGROUND_CHANGED,
                            signature = backgroundPath ?: "<cleared>",
                            text =
                                    if (backgroundPath == null) {
                                        "Live2D 背景已清除。"
                                    } else {
                                        "Live2D 背景已更新。"
                                    },
                            source = SOURCE_ANDROID_APP,
                            priority = TriggerPriority.LOW,
                            metadata = mapOf("background_path" to backgroundPath)
                    )
        }
        update.interaction?.let { interaction ->
            submissions +=
                    emitInteraction(update = update, context = context, interaction = interaction)
        }
        if (update.hasVisualSnapshot) {
            val snapshot = update.visualSnapshot
            if (snapshot != null) {
                submissions += emitVisualSnapshot(context, snapshot)
            }
        }
        return submissions
    }

    private fun emitInteraction(
            update: ChatEnvironmentUpdate,
            context: ChatEnvironmentTriggerContext,
            interaction: ChatEnvironmentInteraction
    ): List<EnvironmentTriggerSubmission> {
        val type = interaction.type.cleanOrNull() ?: return emptyList()
        val x = interaction.x?.takeIf { it.isFinite() }
        val y = interaction.y?.takeIf { it.isFinite() }
        val timestamp = interaction.timestampMillis?.takeIf { it >= 0L } ?: clockMillis()
        val source =
                if (type.startsWith("wallpaper_")) {
                    SOURCE_ANDROID_WALLPAPER
                } else {
                    SOURCE_ANDROID_LIVE2D
                }
        val position =
                if (x != null && y != null) {
                    "，位置 x=${x.oneDecimal()}, y=${y.oneDecimal()}"
                } else {
                    ""
                }
        return emit(
                context = context,
                eventType = EVENT_LIVE2D_INTERACTION,
                signature = signatureOf(type, x?.oneDecimal(), y?.oneDecimal(), timestamp),
                text = "检测到 Live2D 交互：$type$position。",
                source = source,
                priority = TriggerPriority.NORMAL,
                metadata =
                        mapOf(
                                "interaction_type" to type,
                                "interaction_x" to x,
                                "interaction_y" to y,
                                "interaction_timestamp_millis" to timestamp,
                                "lifecycle_state" to update.lifecycleState
                        )
        )
    }

    private fun emitVisualSnapshot(
            context: ChatEnvironmentTriggerContext,
            snapshot: ChatEnvironmentVisualSnapshot
    ): List<EnvironmentTriggerSubmission> {
        val reference = snapshot.reference.cleanOrNull() ?: return emptyList()
        val source =
                if (reference.startsWith("$SOURCE_ANDROID_WALLPAPER:")) {
                    SOURCE_ANDROID_WALLPAPER
                } else {
                    SOURCE_ANDROID_APP
                }
        val dimensions =
                if (snapshot.width != null && snapshot.height != null) {
                    "${snapshot.width}x${snapshot.height}"
                } else {
                    "未知尺寸"
                }
        return emit(
                context = context,
                eventType = EVENT_VISUAL_SNAPSHOT_READY,
                signature =
                        signatureOf(
                                reference,
                                snapshot.mimeType,
                                snapshot.width,
                                snapshot.height
                        ),
                text = "可视环境快照已更新：$dimensions。",
                source = source,
                priority = TriggerPriority.LOW,
                metadata =
                        mapOf(
                                "visual_snapshot_reference" to reference,
                                "visual_snapshot_mime_type" to snapshot.mimeType,
                                "visual_snapshot_width" to snapshot.width,
                                "visual_snapshot_height" to snapshot.height,
                                "visual_snapshot_captured_at_millis" to
                                        snapshot.capturedAtMillis
                        )
        )
    }

    private fun emit(
            context: ChatEnvironmentTriggerContext,
            eventType: String,
            signature: String,
            text: String,
            source: String,
            priority: TriggerPriority,
            metadata: Map<String, Any?> = emptyMap()
    ): List<EnvironmentTriggerSubmission> {
        if (!shouldEmit(eventType, signature)) return emptyList()
        return listOf(
                EnvironmentTriggerSubmission(
                        routingKey = context.routingKey,
                        text = text,
                        priority = priority,
                        timestampSeconds = clockMillis() / 1000.0,
                        source = source,
                        metadata =
                                compactMetadata(
                                        baseMetadata(context, eventType, signature, source) +
                                                metadata
                                )
                )
        )
    }

    private fun shouldEmit(eventType: String, signature: String): Boolean =
            synchronized(emissionLock) {
                val now = clockMillis()
                val last = lastEmissions[eventType]
                if (last?.signature == signature) return@synchronized false
                val minInterval = minIntervalsMillis[eventType] ?: 0L
                if (last != null && now - last.timestampMillis < minInterval) {
                    return@synchronized false
                }
                lastEmissions[eventType] =
                        Emission(signature = signature, timestampMillis = now)
                true
            }

    private fun baseMetadata(
            context: ChatEnvironmentTriggerContext,
            eventType: String,
            signature: String,
            source: String
    ): Map<String, Any?> =
            mapOf(
                    "event_type" to eventType,
                    "event_signature" to signature,
                    "model_key" to context.modelKey,
                    "model_name" to context.modelName,
                    "sender_id" to source,
                    "receiver_user_id" to context.userId,
                    "receiver_user_name" to context.userName,
                    "agent_user_id" to
                            (context.agentUserId.cleanOrNull() ?: context.routingKey.agentId),
                    "agent_user_name" to context.agentName
            )

    private data class Emission(val signature: String, val timestampMillis: Long)

    companion object {
        const val EVENT_MODEL_CHANGED = "model_changed"
        const val EVENT_APP_VISIBLE = "app_visible"
        const val EVENT_WALLPAPER_VISIBLE = "wallpaper_visible"
        const val EVENT_BACKGROUND_CHANGED = "background_changed"
        const val EVENT_LIVE2D_INTERACTION = "live2d_interaction"
        const val EVENT_VISUAL_SNAPSHOT_READY = "visual_snapshot_ready"

        const val SOURCE_ANDROID_APP = "android_app"
        const val SOURCE_ANDROID_WALLPAPER = "android_wallpaper"
        const val SOURCE_ANDROID_LIVE2D = "android_live2d"

        val DEFAULT_MIN_INTERVALS_MILLIS =
                mapOf(
                        EVENT_MODEL_CHANGED to 1_000L,
                        EVENT_APP_VISIBLE to 30_000L,
                        EVENT_WALLPAPER_VISIBLE to 30_000L,
                        EVENT_BACKGROUND_CHANGED to 5_000L,
                        EVENT_LIVE2D_INTERACTION to 500L,
                        EVENT_VISUAL_SNAPSHOT_READY to 10_000L
                )
    }
}

private fun compactMetadata(metadata: Map<String, Any?>): Map<String, Any?> =
        metadata
                .mapNotNull { (key, value) ->
                    val normalized =
                            when (value) {
                                is String -> value.cleanOrNull()
                                else -> value
                            }
                    normalized?.let { key to it }
                }
                .toMap()

private fun signatureOf(vararg values: Any?): String =
        values.joinToString("|") { value -> value?.toString()?.ifBlank { "<blank>" } ?: "<null>" }

private fun Float.oneDecimal(): String = String.format(Locale.US, "%.1f", this)

private fun String?.cleanOrNull(): String? = this?.trim()?.takeIf { it.isNotBlank() }
