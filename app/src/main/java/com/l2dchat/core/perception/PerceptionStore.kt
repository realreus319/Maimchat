package com.l2dchat.core.perception

interface PerceptionStore {
    suspend fun persist(parsedMessage: ParsedMessage)
}
