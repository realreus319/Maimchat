package com.l2dchat.chat.service

import android.content.SharedPreferences

enum class ChatRuntimeMode(val wireValue: String) {
    LOCAL("local"),
    REMOTE("remote");

    companion object {
        fun fromWireValue(value: String?): ChatRuntimeMode? =
                when (value?.trim()?.lowercase()) {
                    LOCAL.wireValue -> LOCAL
                    REMOTE.wireValue -> REMOTE
                    else -> null
                }
    }
}

internal object ChatRuntimeModeStore {
    const val KEY_RUNTIME_MODE = "runtime_mode"

    fun read(prefs: SharedPreferences): ChatRuntimeMode =
            ChatRuntimeMode.fromWireValue(prefs.getString(KEY_RUNTIME_MODE, null))
                    ?: ChatRuntimeMode.LOCAL

    fun persist(prefs: SharedPreferences, mode: ChatRuntimeMode) {
        prefs.edit().putString(KEY_RUNTIME_MODE, mode.wireValue).apply()
    }
}
