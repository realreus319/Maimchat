package com.l2dchat.core.context

data class RoutingKey(val contextId: String, val agentId: String) {
    init {
        require(contextId.isNotBlank()) { "contextId must not be blank" }
        require(agentId.isNotBlank()) { "agentId must not be blank" }
    }

    val loopId: String
        get() = "$contextId:$agentId"

    companion object {
        const val DEFAULT_CONTEXT_ID = "default"
        const val DEFAULT_AGENT_ID = "local_agent"
    }
}
