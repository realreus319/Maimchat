package com.l2dchat.chat

data class MotionCommand(
        val group: String? = null,
        val index: Int? = null,
        val filePath: String? = null,
        val loop: Boolean = false
) {
    init {
        require(group == null || group.isNotBlank()) { "Motion group must not be blank" }
        require(index == null || index >= 0) { "Motion index must be non-negative" }
        require(filePath == null || filePath.isNotBlank()) {
            "Motion file path must not be blank"
        }
        require(filePath != null || group != null) {
            "Motion command requires file path or group"
        }
        require(group == null || index != null) {
            "Motion command requires index when group is provided"
        }
    }
}

object MotionMessage {
    const val TYPE: String = "motion"
    const val KEY_TYPE: String = "message_type"
    const val KEY_GROUP: String = "motion_group"
    const val KEY_INDEX: String = "motion_index"
    const val KEY_FILE_PATH: String = "motion_file_path"
    const val KEY_LOOP: String = "motion_loop"

    fun additionalConfig(command: MotionCommand): Map<String, Any> =
            buildMap {
                put(KEY_TYPE, TYPE)
                command.group?.let { put(KEY_GROUP, it) }
                command.index?.let { put(KEY_INDEX, it) }
                command.filePath?.let { put(KEY_FILE_PATH, it) }
                put(KEY_LOOP, command.loop)
            }

    fun parse(message: MessageBase): MotionCommand? {
        val config = message.messageInfo.additionalConfig ?: return null
        val messageType = config[KEY_TYPE]?.toString()?.lowercase()
        if (messageType != TYPE) return null

        val group = config[KEY_GROUP].asOptionalString()
        val index = config[KEY_INDEX].asOptionalInt()
        val filePath = config[KEY_FILE_PATH].asOptionalString()
        val loop = config[KEY_LOOP].asOptionalBoolean() ?: false
        return runCatching {
                    MotionCommand(
                            group = group,
                            index = index,
                            filePath = filePath,
                            loop = loop
                    )
                }
                .getOrNull()
    }

    fun displayText(command: MotionCommand): String =
            when {
                command.group != null && command.index != null ->
                        "Play motion: ${command.group}[${command.index}]"
                command.filePath != null -> "Play motion: ${command.filePath}"
                else -> "Play motion"
            }

    private fun Any?.asOptionalString(): String? =
            this?.toString()?.trim()?.takeIf { it.isNotEmpty() }

    private fun Any?.asOptionalInt(): Int? =
            when (this) {
                null -> null
                is Number -> toInt()
                is String -> trim().toIntOrNull()
                else -> toString().trim().toIntOrNull()
            }

    private fun Any?.asOptionalBoolean(): Boolean? =
            when (this) {
                null -> null
                is Boolean -> this
                is String -> trim().lowercase().toBooleanStrictOrNull()
                else -> toString().trim().lowercase().toBooleanStrictOrNull()
            }
}
