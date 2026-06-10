package com.l2dchat.ui.screens

import com.l2dchat.core.config.LocalLlmSettings

object RuntimeSettingsValidation {
    fun validateConfig(
            url: String,
            nickname: String,
            localLlmSettings: LocalLlmSettings = LocalLlmSettings()
    ): List<String> {
        val errors = mutableListOf<String>()
        if (nickname.isBlank()) errors.add("昵称不能为空")
        if (localLlmSettings.enabled) {
            errors += validateLocalProviderConfig(localLlmSettings)
        } else {
            val serverUrl = url.trim()
            if (serverUrl.isBlank()) errors.add("WebSocket 地址不能为空")
            else if (!(serverUrl.startsWith("ws://") || serverUrl.startsWith("wss://")))
                    errors.add("WebSocket 地址必须以 ws:// 或 wss:// 开头")
        }
        return errors
    }

    fun validateLocalLlmNumericInput(
            enabled: Boolean,
            temperature: String,
            maxTokens: String,
            timeoutMillis: String
    ): List<String> {
        if (!enabled) return emptyList()
        val errors = mutableListOf<String>()
        val rawTemperature = temperature.trim()
        if (rawTemperature.isNotEmpty()) {
            val parsed = rawTemperature.toDoubleOrNull()
            if (parsed == null || !parsed.isFinite()) {
                errors.add("Temperature 必须是数字")
            }
        }
        val rawMaxTokens = maxTokens.trim()
        if (rawMaxTokens.isNotEmpty() && (rawMaxTokens.toIntOrNull() ?: 0) <= 0) {
            errors.add("Max tokens 必须是正整数")
        }
        val rawTimeout = timeoutMillis.trim()
        if (rawTimeout.isNotEmpty() && (rawTimeout.toLongOrNull() ?: 0L) <= 0L) {
            errors.add("超时必须是正整数毫秒")
        }
        return errors
    }

    private fun validateLocalProviderConfig(localLlmSettings: LocalLlmSettings): List<String> {
        if (!localLlmSettings.hasProviderInput()) return emptyList()
        val errors = mutableListOf<String>()
        val baseUrl = localLlmSettings.baseUrl.orEmpty().trim()
        if (baseUrl.isBlank()) {
            errors.add("LLM Endpoint 不能为空")
        } else if (!(baseUrl.startsWith("http://") || baseUrl.startsWith("https://"))) {
            errors.add("LLM Endpoint 必须以 http:// 或 https:// 开头")
        }
        if (localLlmSettings.plannerModel.isNullOrBlank()) {
            errors.add("Planner 模型不能为空")
        }
        return errors
    }

    private fun LocalLlmSettings.hasProviderInput(): Boolean =
            listOf(baseUrl, apiKey, plannerModel, replierModel).any { !it.isNullOrBlank() }
}
