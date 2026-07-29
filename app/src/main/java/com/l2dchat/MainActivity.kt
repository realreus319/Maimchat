package com.l2dchat

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.tooling.preview.Preview
import com.l2dchat.chat.service.ChatServiceClient
import com.l2dchat.live2d.ImprovedLive2DRenderer
import com.l2dchat.live2d.Live2DModelManager
import com.l2dchat.preferences.ChatPreferenceKeys
import com.l2dchat.ui.screens.ChatWithModelScreen
import com.l2dchat.ui.screens.ModelSelectionDialog
import com.l2dchat.ui.theme.L2DChatTheme
import com.l2dchat.worker.ShellEngineClient
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        ImprovedLive2DRenderer.ensureFrameworkInitialized()
        setContent { L2DChatTheme { Live2DChatApp() } }
    }
    override fun onDestroy() {
        ImprovedLive2DRenderer.safeShutdownFramework()
        super.onDestroy()
    }
}

@Composable
fun Live2DChatApp() {
    var currentScreen by remember { mutableStateOf(ChatAppScreen.ModelChat) }
    var selectedModel by remember { mutableStateOf<Live2DModelManager.ModelInfo?>(null) }
    var showModelSelection by remember { mutableStateOf(false) }
    val context = androidx.compose.ui.platform.LocalContext.current
    val chatManager = remember { ChatServiceClient(context.applicationContext) }
    DisposableEffect(chatManager) {
        chatManager.bindService()
        onDispose { chatManager.release() }
    }
    var modelKey by remember { mutableStateOf(0) }
    val prefs =
            remember(context) {
                context.getSharedPreferences(
                        ChatPreferenceKeys.PREFS_NAME,
                        android.content.Context.MODE_PRIVATE
                )
            }
    val persistModelSelection =
            remember(prefs) {
                { model: Live2DModelManager.ModelInfo? ->
                    prefs.edit()
                            .also { editor ->
                                if (model != null) {
                                    editor.putString(
                                            ChatPreferenceKeys.SELECTED_MODEL_FOLDER,
                                            model.folderPath
                                    )
                                } else {
                                    editor.remove(ChatPreferenceKeys.SELECTED_MODEL_FOLDER)
                                }
                            }
                            .apply()
                }
            }

    // 自动连接逻辑：读取偏好并在首次组合时尝试连接
    LaunchedEffect(Unit) {
        chatManager.ensureBound()
        val nickname = prefs.getString("nickname", null)
        if (!nickname.isNullOrBlank()) {
            chatManager.setUserProfile(nickname)
        }
        if (selectedModel == null) {
            val savedModelFolder = prefs.getString(ChatPreferenceKeys.SELECTED_MODEL_FOLDER, null)
            if (!savedModelFolder.isNullOrBlank()) {
                val models = Live2DModelManager.scanModels(context)
                val matched = models.firstOrNull { it.folderPath == savedModelFolder }
                if (matched != null) {
                    selectedModel = matched
                    modelKey++
                }
            }
        }
    }
    // First launch: pre-warm the worker engine WHILE we're foreground (so the proot rootfs extracts
    // before the first AI-agent call and isn't a cold start the ROM kills), and prompt once for the
    // "draw over other apps" permission the worker's web browser needs.
    var showOverlayPrompt by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        launch { runCatching { ShellEngineClient(context.applicationContext).warmUp() } }
        if (!Settings.canDrawOverlays(context)) showOverlayPrompt = true
    }
    when (currentScreen) {
        ChatAppScreen.ModelChat ->
                ChatWithModelScreen(
                        selectedModel = selectedModel,
                        chatManager = chatManager,
                        modelKey = modelKey,
                        onModelSelectionRequest = { showModelSelection = true },
                        onModelChanged = { newModel ->
                            selectedModel = newModel
                            persistModelSelection(newModel)
                            modelKey++
                        }
                )
    }
    if (showModelSelection) {
        ModelSelectionDialog(
                currentModel = selectedModel,
                onModelSelected = { m ->
                    selectedModel = m
                    persistModelSelection(m)
                    modelKey++
                    showModelSelection = false
                },
                onDismiss = { showModelSelection = false }
        )
    }
    if (showOverlayPrompt) {
        AlertDialog(
                onDismissRequest = { showOverlayPrompt = false },
                title = { Text("需要开启权限") },
                text = {
                    Text(
                            "为让 AI 智能体正常工作，请在本应用的设置页开启：\n\n" +
                                    "① 自启动 / 关联启动 —— 否则系统会拦截智能体引擎的启动，所有 AI 任务都无法运行。\n" +
                                    "② 显示在其他应用上层（悬浮窗）—— 网页搜索/浏览任务需要。\n" +
                                    "③ 建议在“耗电管理”里关闭对本应用的后台限制。\n\n" +
                                    "点「去设置」打开本应用设置页逐项开启。"
                    )
                },
                confirmButton = {
                    TextButton(
                            onClick = {
                                showOverlayPrompt = false
                                // App-info page is the ColorOS hub for 自启动 + 耗电 + 权限/悬浮窗; fall
                                // back to the dedicated overlay-permission screen if it's unavailable.
                                val opened =
                                        runCatching {
                                                    context.startActivity(
                                                            Intent(
                                                                            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                                                            Uri.parse("package:${context.packageName}")
                                                                    )
                                                                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                                                    )
                                                }
                                                .isSuccess
                                if (!opened) {
                                    runCatching {
                                        context.startActivity(
                                                Intent(
                                                                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                                                Uri.parse("package:${context.packageName}")
                                                        )
                                                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                                        )
                                    }
                                }
                            }
                    ) { Text("去设置") }
                },
                dismissButton = {
                    TextButton(onClick = { showOverlayPrompt = false }) { Text("暂不") }
                }
        )
    }
}

enum class ChatAppScreen {
    ModelChat
}

@Preview
@Composable
fun PreviewApp() {
    L2DChatTheme { Live2DChatApp() }
}
