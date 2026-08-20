package com.l2dchat.chat.service

import com.l2dchat.core.config.MemSecretStore

/**
 * Bridges the app's encrypted [SecurePreferenceStore] (internal to this
 * package) to the public [MemSecretStore] contract declared in `core.config`.
 *
 * [MemSettingsStore] depends on [MemSecretStore] (not on [SecretStringStore])
 * so the `core.config` layer stays free of `chat.service` types. This adapter
 * is the single place where the two meet: it wraps a [SecurePreferenceStore]
 * instance and exposes it under the [MemSecretStore] interface for
 * [MemSettingsStore.read] / [MemSettingsStore.persist] to use.
 *
 * Lives in `chat.service` (next to [SecurePreferenceStore] and the other
 * settings wiring) because [SecurePreferenceStore] is `internal` to this
 * package — the adapter could not see it from anywhere else.
 */
internal class SecurePreferenceMemSecretStore(
    private val delegate: SecretStringStore,
) : MemSecretStore {
    override fun getString(key: String): String? = delegate.getString(key)
    override fun putString(key: String, value: String?): Boolean = delegate.putString(key, value)
}
