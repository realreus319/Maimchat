package com.l2dchat.core.perception

data class ParserConfig(
        val commandPrefix: String = "/",
        // A mention is "@name" at a word boundary — the `@` must not be preceded by a word
        // character, so email addresses like "a@b.com" are NOT treated as mentions (which would
        // otherwise spuriously elevate the message to HIGH priority).
        val mentionPatterns: List<String> = listOf("""(?<![\w@])@(\w+)"""),
        val commandPatterns: List<String> = emptyList()
)
