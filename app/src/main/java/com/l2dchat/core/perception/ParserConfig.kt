package com.l2dchat.core.perception

data class ParserConfig(
        val commandPrefix: String = "/",
        val mentionPatterns: List<String> = listOf("""@(\w+)""", """@(\S+)"""),
        val commandPatterns: List<String> = emptyList()
)
