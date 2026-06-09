package com.l2dchat.core.perception

import com.l2dchat.core.inbound.InboundMessage

class DefaultMessageParser(private val config: ParserConfig = ParserConfig()) : MessageParser {
    private val mentionRegexes = config.mentionPatterns.map { Regex(it) }
    private val commandRegex =
            config.commandPatterns.firstNotNullOfOrNull { pattern ->
                runCatching { Regex(pattern) }.getOrNull()
            }
                    ?: Regex("^${Regex.escape(config.commandPrefix)}(\\w+)(?:\\s+(.*))?$")

    override fun parse(message: InboundMessage): ParsedMessage {
        val text = message.text
        val trimmed = text.trim()
        var isCommand = false
        var commandName: String? = null
        var commandArgs: Map<String, Any> = emptyMap()

        val match = commandRegex.matchEntire(trimmed)
        if (match != null) {
            isCommand = true
            commandName = match.groupValues.getOrNull(1)?.takeIf { it.isNotBlank() }
            val argString = match.groupValues.getOrNull(2)?.takeIf { it.isNotBlank() }
            if (argString != null) {
                commandArgs = parseCommandArgs(argString)
            }
        }

        if (!isCommand && text.startsWith(config.commandPrefix)) {
            isCommand = true
            val parts = text.removePrefix(config.commandPrefix).split(Regex("\\s+"), limit = 2)
            commandName = parts.getOrNull(0)?.takeIf { it.isNotBlank() }
            commandArgs =
                    parts.getOrNull(1)?.takeIf { it.isNotBlank() }?.let {
                        mapOf("args" to it)
                    } ?: emptyMap()
        }

        val mentionedNames = linkedSetOf<String>()
        mentionRegexes.forEach { regex ->
            regex.findAll(text).forEach { result ->
                val value =
                        result.groupValues.getOrNull(1)?.takeIf { it.isNotBlank() }
                                ?: result.value.removePrefix("@")
                if (value.isNotBlank()) {
                    mentionedNames.add(value)
                }
            }
        }

        return ParsedMessage(
                messageId = message.messageId,
                contextId = message.routingKey.contextId,
                agentId = message.routingKey.agentId,
                text = text,
                senderId = message.senderId,
                senderName = message.senderName,
                isMentioned = mentionedNames.isNotEmpty(),
                mentionedNames = mentionedNames.toList(),
                isCommand = isCommand,
                commandName = commandName,
                commandArgs = commandArgs,
                contentBlocks = message.contentBlocks,
                timestampSeconds = message.timestampSeconds,
                rawMessage = message
        )
    }

    private fun parseCommandArgs(argString: String): Map<String, Any> {
        val args = linkedMapOf<String, Any>()
        val matches = COMMAND_KV_REGEX.findAll(argString).toList()
        var remaining = argString

        matches.asReversed().forEach { match ->
            val key = match.groupValues[1]
            val value = match.groupValues[2].trimMatchingQuotes()
            args[key] = value
            remaining = remaining.removeRange(match.range)
        }

        val positional = remaining.trim().split(Regex("\\s+")).filter { it.isNotBlank() }
        if (positional.isNotEmpty()) {
            args["args"] = positional.joinToString(" ")
        }

        return args.ifEmpty { mapOf("args" to argString) }
    }

    private fun String.trimMatchingQuotes(): String =
            if (length >= 2 &&
                            ((startsWith("\"") && endsWith("\"")) ||
                                    (startsWith("'") && endsWith("'")))
            ) {
                substring(1, length - 1)
            } else {
                this
            }

    companion object {
        private val COMMAND_KV_REGEX = Regex("""(\w+)=("[^"]*"|'[^']*'|\S+)""")
    }
}
