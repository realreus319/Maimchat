package com.l2dchat.chat

import android.content.Context
import android.content.SharedPreferences
import com.l2dchat.core.environment.EnvironmentChatBubble
import com.l2dchat.core.environment.EnvironmentInteraction
import com.l2dchat.core.environment.EnvironmentModelState
import com.l2dchat.core.environment.EnvironmentMotion
import com.l2dchat.core.environment.EnvironmentState
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentSurfaceState
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.wallpaper.WallpaperComm

data class ChatEnvironmentUpdate(
        val modelKey: String? = null,
        val modelName: String? = null,
        val modelFolderPath: String? = null,
        val modelFile: String? = null,
        val lifecycleState: String? = null,
        val motionFiles: List<String>? = null,
        val appVisible: Boolean? = null,
        val hasAppVisible: Boolean = appVisible != null,
        val wallpaperVisible: Boolean? = null,
        val hasWallpaperVisible: Boolean = wallpaperVisible != null,
        val backgroundPath: String? = null,
        val hasBackgroundPath: Boolean = backgroundPath != null,
        val interaction: ChatEnvironmentInteraction? = null
)

data class ChatEnvironmentMessage(
        val text: String,
        val fromUser: Boolean,
        val timestampMillis: Long? = null
)

data class ChatEnvironmentInteraction(
        val type: String,
        val x: Float? = null,
        val y: Float? = null,
        val timestampMillis: Long? = null
)

class ChatEnvironmentStateProvider(appContext: Context? = null) : EnvironmentStateProvider {
    private val lock = Any()
    private var applicationContext: Context? = appContext?.applicationContext
    private var snapshot = Snapshot()

    override fun currentState(context: ToolExecutionContext): EnvironmentState =
            synchronized(lock) {
                snapshot.withWallpaperState(applicationContext).toEnvironmentState(context)
            }

    fun setApplicationContext(context: Context) {
        synchronized(lock) { applicationContext = context.applicationContext }
    }

    fun update(update: ChatEnvironmentUpdate) {
        synchronized(lock) { snapshot = snapshot.updated(update) }
    }

    fun updateRecentMessages(messages: List<ChatEnvironmentMessage>) {
        val bubbles =
                messages
                        .mapNotNull { message ->
                            val text = message.text.trim().takeIf { it.isNotBlank() }
                                    ?: return@mapNotNull null
                            EnvironmentChatBubble(
                                    text = text,
                                    fromUser = message.fromUser,
                                    timestampMillis = message.timestampMillis
                            )
                        }
                        .takeLast(MAX_RECENT_BUBBLES)
        synchronized(lock) {
            snapshot = snapshot.copy(recentBubbles = bubbles, updatedAtMillis = now())
        }
    }

    fun clearRecentMessages() {
        synchronized(lock) {
            snapshot = snapshot.copy(recentBubbles = emptyList(), updatedAtMillis = now())
        }
    }

    private data class Snapshot(
            val model: EnvironmentModelState? = null,
            val motions: List<EnvironmentMotion> = emptyList(),
            val surface: EnvironmentSurfaceState = EnvironmentSurfaceState(),
            val lastInteraction: EnvironmentInteraction? = null,
            val recentBubbles: List<EnvironmentChatBubble> = emptyList(),
            val updatedAtMillis: Long = now()
    ) {
        fun updated(update: ChatEnvironmentUpdate): Snapshot {
            val nextModel = updateModel(update)
            val nextMotions =
                    update.motionFiles?.let { files -> buildMotionList(files) } ?: motions
            val nextSurface =
                    EnvironmentSurfaceState(
                            appVisible =
                                    if (update.hasAppVisible) update.appVisible
                                    else surface.appVisible,
                            wallpaperVisible =
                                    if (update.hasWallpaperVisible) update.wallpaperVisible
                                    else surface.wallpaperVisible,
                            backgroundPath =
                                    if (update.hasBackgroundPath) update.backgroundPath.cleanOrNull()
                                    else surface.backgroundPath
                    )
            return copy(
                    model = nextModel,
                    motions = nextMotions,
                    surface = nextSurface,
                    lastInteraction =
                            update.interaction?.toEnvironmentInteraction() ?: lastInteraction,
                    updatedAtMillis = now()
            )
        }

        private fun updateModel(update: ChatEnvironmentUpdate): EnvironmentModelState? {
            if (!update.hasModelData()) return model
            val key =
                    update.modelKey.cleanOrNull()
                            ?: model?.key
                            ?: stableModelKey(update.modelFolderPath, update.modelName)
            val name = update.modelName.cleanOrNull() ?: model?.name
            val folderPath = update.modelFolderPath.cleanOrNull() ?: model?.folderPath
            val lifecycleState = update.lifecycleState.cleanOrNull() ?: model?.lifecycleState
            if (key == null && name == null && folderPath == null && lifecycleState == null) {
                return null
            }
            return EnvironmentModelState(
                    key = key,
                    name = name,
                    folderPath = folderPath,
                    lifecycleState = lifecycleState
            )
        }

        fun withWallpaperState(context: Context?): Snapshot {
            val prefs =
                    context?.getSharedPreferences(
                            WallpaperComm.PREF_WALLPAPER,
                            Context.MODE_PRIVATE
                    )
                            ?: return this
            val wallpaperVisible =
                    if (prefs.contains(WallpaperComm.PREF_WALLPAPER_VISIBLE)) {
                        prefs.getBoolean(WallpaperComm.PREF_WALLPAPER_VISIBLE, false)
                    } else {
                        null
                    }
            val wallpaperInteraction = prefs.wallpaperInteraction()
            val wallpaperUpdatedAt =
                    listOfNotNull(
                                    prefs.optionalLong(
                                            WallpaperComm.PREF_WALLPAPER_VISIBLE_UPDATED_AT
                                    ),
                                    wallpaperInteraction?.timestampMillis
                            )
                            .maxOrNull()
            return copy(
                    surface =
                            if (wallpaperVisible == null) surface
                            else surface.copy(wallpaperVisible = wallpaperVisible),
                    lastInteraction = newestInteraction(lastInteraction, wallpaperInteraction),
                    updatedAtMillis = maxOf(updatedAtMillis, wallpaperUpdatedAt ?: updatedAtMillis)
            )
        }

        fun toEnvironmentState(context: ToolExecutionContext): EnvironmentState =
                EnvironmentState(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        model = model,
                        motions = motions,
                        surface = surface,
                        lastInteraction = lastInteraction,
                        recentBubbles = recentBubbles,
                        metadata =
                                mapOf(
                                        "provider" to "chat_environment",
                                        "updated_at_millis" to updatedAtMillis
                                )
                )
    }

    companion object {
        private const val MAX_RECENT_BUBBLES = 50
    }
}

private data class MotionIdentity(val group: String, val index: Int)

private fun ChatEnvironmentUpdate.hasModelData(): Boolean =
        modelKey.cleanOrNull() != null ||
                modelName.cleanOrNull() != null ||
                modelFolderPath.cleanOrNull() != null ||
                modelFile.cleanOrNull() != null ||
                lifecycleState.cleanOrNull() != null

private fun buildMotionList(files: List<String>): List<EnvironmentMotion> =
        files
                .mapIndexedNotNull { fallbackIndex, rawPath ->
                    val filePath = rawPath.cleanOrNull() ?: return@mapIndexedNotNull null
                    val identity = parseMotionIdentity(filePath, fallbackIndex)
                    EnvironmentMotion(
                            group = identity.group,
                            index = identity.index,
                            filePath = filePath,
                            displayName = displayNameForMotion(filePath)
                    )
                }
                .distinctBy { "${it.group}:${it.index}:${it.filePath}" }

private fun parseMotionIdentity(filePath: String, fallbackIndex: Int): MotionIdentity {
    val fileName = filePath.substringAfterLast('/')
    val base =
            fileName
                    .removeSuffix(".motion3.json")
                    .removeSuffix(".motion3")
                    .removeSuffix(".json")
    val indexed = Regex("^(.+?)[_-]?(?:m|motion)?(\\d+)$", RegexOption.IGNORE_CASE)
            .matchEntire(base)
    val group =
            indexed
                    ?.groupValues
                    ?.getOrNull(1)
                    ?.trim('_', '-', ' ')
                    ?.cleanOrNull()
                    ?: parentFolderName(filePath)
                    ?: base.cleanOrNull()
                    ?: "Motion"
    val index =
            indexed?.groupValues?.getOrNull(2)?.toIntOrNull()?.takeIf { it >= 0 }
                    ?: fallbackIndex
    return MotionIdentity(group = group, index = index)
}

private fun parentFolderName(filePath: String): String? =
        filePath
                .substringBeforeLast('/', missingDelimiterValue = "")
                .substringAfterLast('/')
                .cleanOrNull()

private fun displayNameForMotion(filePath: String): String {
    val fileName = filePath.substringAfterLast('/')
    val base =
            fileName
                    .removeSuffix(".motion3.json")
                    .removeSuffix(".motion3")
                    .removeSuffix(".json")
                    .replace('_', ' ')
                    .replace('-', ' ')
                    .trim()
    return base.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }
}

private fun stableModelKey(vararg candidates: String?): String? =
        candidates.mapNotNull { it.cleanOrNull() }.firstOrNull()

private fun SharedPreferences.wallpaperInteraction(): EnvironmentInteraction? {
    val type = getString(WallpaperComm.PREF_WALLPAPER_INTERACTION_TYPE, null).cleanOrNull()
            ?: return null
    return EnvironmentInteraction(
            type = type,
            x = optionalFloat(WallpaperComm.PREF_WALLPAPER_INTERACTION_X),
            y = optionalFloat(WallpaperComm.PREF_WALLPAPER_INTERACTION_Y),
            timestampMillis = optionalLong(WallpaperComm.PREF_WALLPAPER_INTERACTION_TIMESTAMP)
    )
}

private fun SharedPreferences.optionalFloat(key: String): Float? =
        if (contains(key)) getFloat(key, 0f).takeIf { it.isFinite() } else null

private fun SharedPreferences.optionalLong(key: String): Long? =
        if (contains(key)) getLong(key, 0L).takeIf { it >= 0L } else null

private fun newestInteraction(
        current: EnvironmentInteraction?,
        candidate: EnvironmentInteraction?
): EnvironmentInteraction? {
    if (candidate == null) return current
    if (current == null) return candidate
    val currentTimestamp = current.timestampMillis
    val candidateTimestamp = candidate.timestampMillis
    if (candidateTimestamp == null) return current
    if (currentTimestamp == null) return candidate
    return if (candidateTimestamp >= currentTimestamp) candidate else current
}

private fun ChatEnvironmentInteraction.toEnvironmentInteraction(): EnvironmentInteraction? {
    val cleanType = type.cleanOrNull() ?: return null
    return EnvironmentInteraction(
            type = cleanType,
            x = x?.takeIf { it.isFinite() },
            y = y?.takeIf { it.isFinite() },
            timestampMillis = timestampMillis?.takeIf { it >= 0L }
    )
}

private fun String?.cleanOrNull(): String? = this?.trim()?.takeIf { it.isNotBlank() }

private fun now(): Long = System.currentTimeMillis()
