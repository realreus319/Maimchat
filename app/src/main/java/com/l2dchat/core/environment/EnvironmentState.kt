package com.l2dchat.core.environment

import com.l2dchat.core.tools.ToolExecutionContext

data class EnvironmentState(
        val contextId: String,
        val agentId: String,
        val model: EnvironmentModelState? = null,
        val motions: List<EnvironmentMotion> = emptyList(),
        val expression: String? = null,
        val surface: EnvironmentSurfaceState = EnvironmentSurfaceState(),
        val lastInteraction: EnvironmentInteraction? = null,
        val recentBubbles: List<EnvironmentChatBubble> = emptyList(),
        val visualSnapshot: EnvironmentVisualSnapshot? = null,
        val metadata: Map<String, Any?> = emptyMap()
) {
    init {
        require(contextId.isNotBlank()) { "Environment contextId must not be blank" }
        require(agentId.isNotBlank()) { "Environment agentId must not be blank" }
        require(expression == null || expression.isNotBlank()) {
            "Environment expression must not be blank"
        }
    }
}

data class EnvironmentModelState(
        val key: String? = null,
        val name: String? = null,
        val folderPath: String? = null,
        val lifecycleState: String? = null
) {
    init {
        require(key == null || key.isNotBlank()) { "Environment model key must not be blank" }
        require(name == null || name.isNotBlank()) { "Environment model name must not be blank" }
        require(folderPath == null || folderPath.isNotBlank()) {
            "Environment model folderPath must not be blank"
        }
        require(lifecycleState == null || lifecycleState.isNotBlank()) {
            "Environment model lifecycleState must not be blank"
        }
    }
}

data class EnvironmentMotion(
        val group: String,
        val index: Int,
        val filePath: String? = null,
        val displayName: String? = null
) {
    init {
        require(group.isNotBlank()) { "Environment motion group must not be blank" }
        require(index >= 0) { "Environment motion index must be non-negative" }
        require(filePath == null || filePath.isNotBlank()) {
            "Environment motion filePath must not be blank"
        }
        require(displayName == null || displayName.isNotBlank()) {
            "Environment motion displayName must not be blank"
        }
    }
}

data class EnvironmentSurfaceState(
        val appVisible: Boolean? = null,
        val wallpaperVisible: Boolean? = null,
        val backgroundPath: String? = null
) {
    init {
        require(backgroundPath == null || backgroundPath.isNotBlank()) {
            "Environment backgroundPath must not be blank"
        }
    }
}

data class EnvironmentInteraction(
        val type: String,
        val x: Float? = null,
        val y: Float? = null,
        val timestampMillis: Long? = null
) {
    init {
        require(type.isNotBlank()) { "Environment interaction type must not be blank" }
        require(timestampMillis == null || timestampMillis >= 0L) {
            "Environment interaction timestampMillis must be non-negative"
        }
    }
}

data class EnvironmentChatBubble(
        val text: String,
        val fromUser: Boolean,
        val timestampMillis: Long? = null
) {
    init {
        require(text.isNotBlank()) { "Environment chat bubble text must not be blank" }
        require(timestampMillis == null || timestampMillis >= 0L) {
            "Environment chat bubble timestampMillis must be non-negative"
        }
    }
}

data class EnvironmentVisualSnapshot(
        val reference: String,
        val mimeType: String? = null,
        val width: Int? = null,
        val height: Int? = null,
        val capturedAtMillis: Long? = null
) {
    init {
        require(reference.isNotBlank()) { "Environment visual snapshot reference must not be blank" }
        require(mimeType == null || mimeType.isNotBlank()) {
            "Environment visual snapshot mimeType must not be blank"
        }
        require(width == null || width > 0) { "Environment visual snapshot width must be positive" }
        require(height == null || height > 0) {
            "Environment visual snapshot height must be positive"
        }
        require(capturedAtMillis == null || capturedAtMillis >= 0L) {
            "Environment visual snapshot capturedAtMillis must be non-negative"
        }
    }
}

fun interface EnvironmentStateProvider {
    fun currentState(context: ToolExecutionContext): EnvironmentState
}

object EmptyEnvironmentStateProvider : EnvironmentStateProvider {
    override fun currentState(context: ToolExecutionContext): EnvironmentState =
            EnvironmentState(
                    contextId = context.routingKey.contextId,
                    agentId = context.routingKey.agentId
            )
}
