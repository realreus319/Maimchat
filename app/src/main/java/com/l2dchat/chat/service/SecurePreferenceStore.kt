package com.l2dchat.chat.service

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal interface SecretStringStore {
    fun getString(key: String): String?

    fun putString(key: String, value: String?): Boolean
}

internal class SecurePreferenceStore(context: Context) : SecretStringStore {
    private val prefs: SharedPreferences =
            context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    override fun getString(key: String): String? {
        val encoded = prefs.getString(key, null) ?: return null
        return runCatching { decrypt(encoded) }.getOrNull()
    }

    override fun putString(key: String, value: String?): Boolean {
        val sanitized = value?.takeUnless { it.isBlank() }
        if (sanitized == null) {
            prefs.edit().remove(key).apply()
            return true
        }
        return runCatching {
                    prefs.edit().putString(key, encrypt(sanitized)).apply()
                    true
                }
                .getOrDefault(false)
    }

    private fun encrypt(value: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val cipherText = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        return listOf(FORMAT_VERSION, encode(cipher.iv), encode(cipherText)).joinToString(":")
    }

    private fun decrypt(encoded: String): String {
        val parts = encoded.split(":")
        require(parts.size == 3 && parts[0] == FORMAT_VERSION) { "Unsupported secret format" }
        val iv = decode(parts[1])
        val cipherText = decode(parts[2])
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
        return String(cipher.doFinal(cipherText), Charsets.UTF_8)
    }

    private fun secretKey(): SecretKey =
            synchronized(KEY_LOCK) {
                val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
                (keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.secretKey?.let {
                    return@synchronized it
                }

                val keyGenerator =
                        KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
                val keySpec =
                        KeyGenParameterSpec.Builder(
                                        KEY_ALIAS,
                                        KeyProperties.PURPOSE_ENCRYPT or
                                                KeyProperties.PURPOSE_DECRYPT
                                )
                                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                                .setRandomizedEncryptionRequired(true)
                                .build()
                keyGenerator.init(keySpec)
                keyGenerator.generateKey()
            }

    private fun encode(bytes: ByteArray): String = Base64.encodeToString(bytes, Base64.NO_WRAP)

    private fun decode(value: String): ByteArray = Base64.decode(value, Base64.NO_WRAP)

    companion object {
        private const val PREFS_NAME = "chat_secure_prefs"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "maimchat_chat_secrets_aes"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val FORMAT_VERSION = "v1"
        private const val GCM_TAG_BITS = 128
        private val KEY_LOCK = Any()
    }
}

internal object ChatSecurePreferences {
    const val KEY_AUTH_TOKEN = "auth_token"
    const val KEY_LOCAL_LLM_API_KEY = "local_llm_api_key"

    fun readMigratingString(
            prefs: SharedPreferences,
            secureStore: SecretStringStore,
            key: String
    ): String? {
        val secureValue = secureStore.getString(key)?.takeUnless { it.isBlank() }
        if (secureValue != null) {
            prefs.edit().remove(key).apply()
            return secureValue
        }

        val legacyValue = prefs.getString(key, null)?.takeUnless { it.isBlank() }
        if (prefs.contains(key)) {
            legacyValue?.let { secureStore.putString(key, it) }
            prefs.edit().remove(key).apply()
        }
        return legacyValue
    }

    fun writeString(secureStore: SecretStringStore, key: String, value: String?) {
        secureStore.putString(key, value?.takeUnless { it.isBlank() })
    }
}
