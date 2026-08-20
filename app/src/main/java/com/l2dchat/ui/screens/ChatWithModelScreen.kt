package com.l2dchat.ui.screens

import android.app.Activity
import android.app.WallpaperManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Wallpaper
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.filled.History
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.foundation.clickable
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.ui.draw.clip
import com.l2dchat.browser.BrowserComm
import com.l2dchat.worker.WorkerActivity
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.MotionCommand
import com.l2dchat.chat.service.ChatServiceClient
import com.l2dchat.core.config.AgentProfileRepository
import com.l2dchat.core.config.AgentPromptTemplateNames
import com.l2dchat.core.config.DefaultAgentProfileSeeder
import com.l2dchat.core.config.EditableAgentProfile
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.PersonaRegistry
import com.l2dchat.core.config.WorkerLlmSettings
import com.l2dchat.core.media.LifeCaptureStore
import com.l2dchat.core.storage.ChatDatabase
import com.l2dchat.live2d.ImprovedLive2DRenderer
import com.l2dchat.live2d.Live2DModelLifecycleManager
import com.l2dchat.live2d.Live2DModelManager
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import com.l2dchat.preferences.ChatPreferenceKeys
import com.l2dchat.ui.components.LogViewerDialog
import com.l2dchat.wallpaper.Live2DWallpaperService
import com.l2dchat.wallpaper.WallpaperComm
import com.yalantis.ucrop.UCrop
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream
import kotlin.math.max
import kotlin.math.roundToInt
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

// 固定保留给输入区域的空白高度（模型不绘制到此区域之下）
// 原为 120.dp，按需求缩小约 30% -> 84.dp
private val ReservedBottomHeight = 84.dp
// 顶部 AppBar 高度（防止模型头部被遮或越界），Material3 默认 56.dp
private val TopBarHeight = 56.dp

private const val ENVIRONMENT_VISUAL_SNAPSHOT_MIME_TYPE =
        "application/vnd.l2dchat.environment-snapshot+json"

private data class ConnectionErrorBanner(val id: Long, val message: String)

private data class AgentProfileImportEvent(val id: Long, val json: String)

private val uiLogger = L2DLogger.module(LogModule.MAIN_VIEW)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatWithModelScreen(
        selectedModel: Live2DModelManager.ModelInfo?,
        chatManager: ChatServiceClient,
        modelKey: Int,
        onModelSelectionRequest: () -> Unit,
        onModelChanged: (Live2DModelManager.ModelInfo?) -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    val runtimeState by chatManager.runtimeState.collectAsState()
    val runtimeLabel by chatManager.runtimeLabel.collectAsState()
    val runtimeDiagnostic by chatManager.runtimeDiagnostic.collectAsState()
    val messages by chatManager.messages.collectAsState()
    val isThinking by chatManager.processing.collectAsState()
    val standardMessages by chatManager.standardMessages.collectAsState()
    val currentUserNickname by chatManager.userNickname.collectAsState()
    val personaDisplayName by chatManager.personaDisplayName.collectAsState()
    val selectedPersona by chatManager.selectedPersona.collectAsState()
    val localLlmSettings by chatManager.localLlmSettings.collectAsState()
    val effectiveLocalLlmSettings = localLlmSettings
    val workerLlmSettings by chatManager.workerLlmSettings.collectAsState()
    val prefs =
            remember(context) {
                context.getSharedPreferences(
                        ChatPreferenceKeys.PREFS_NAME,
                        android.content.Context.MODE_PRIVATE
                )
            }
    val wallpaperPrefs =
            remember(context) {
                context.getSharedPreferences(
                        WallpaperComm.PREF_WALLPAPER,
                        android.content.Context.MODE_PRIVATE
                )
            }
    val persistedWallpaperPath =
            remember(wallpaperPrefs) {
                wallpaperPrefs.getString(WallpaperComm.PREF_WALLPAPER_BG_PATH, null)
            }
    var inputText by remember { mutableStateOf("") }
    var showConnectionDialog by remember { mutableStateOf(false) }
    var showWorkerConfigDialog by remember { mutableStateOf(false) }
    var showAgentProfileDialog by remember { mutableStateOf(false) }
    var showPersonaDialog by remember { mutableStateOf(false) }
    var nickname by remember { mutableStateOf(chatManager.getUserNickname() ?: "") }
    var wallpaperBgPath by rememberSaveable { mutableStateOf(persistedWallpaperPath.orEmpty()) }
    var wallpaperTempPath by rememberSaveable { mutableStateOf(persistedWallpaperPath.orEmpty()) }
    var wallpaperBubbleCount by
            rememberSaveable {
                mutableStateOf(
                        wallpaperPrefs.getInt(
                                WallpaperComm.PREF_WALLPAPER_BUBBLE_COUNT,
                                WallpaperComm.DEFAULT_BUBBLE_COUNT
                        )
                )
            }
    var showWallpaperDialog by remember { mutableStateOf(false) }
    var showLogViewer by remember { mutableStateOf(false) }
    var showHistoryViewer by remember { mutableStateOf(false) }
    var backgroundBitmap by remember { mutableStateOf<Bitmap?>(null) }
    var chatInputHeightPx by remember { mutableStateOf(0) }
    var visualSurfaceWidthPx by remember { mutableStateOf(0) }
    var visualSurfaceHeightPx by remember { mutableStateOf(0) }
    val connectionErrorBanners = remember { mutableStateListOf<ConnectionErrorBanner>() }
    var pendingAgentProfileExportJson by remember { mutableStateOf<String?>(null) }
    var agentProfileImportEvent by remember { mutableStateOf<AgentProfileImportEvent?>(null) }
    val runtimeStatusLine =
            remember(
                    runtimeState,
                    runtimeLabel,
                    runtimeDiagnostic,
                    effectiveLocalLlmSettings
            ) {
                buildRuntimeStatusLine(
                        runtimeState = runtimeState,
                        runtimeLabel = runtimeLabel,
                        runtimeDiagnostic = runtimeDiagnostic,
                        localLlmSettings = effectiveLocalLlmSettings
                )
            }

    var isLoadingDefaultModel by remember { mutableStateOf(selectedModel == null) }
    var currentModel by remember(modelKey) { mutableStateOf(selectedModel) }
    var isResetting by remember(modelKey) { mutableStateOf(false) }
    var lifecycleManager by
            remember(modelKey) { mutableStateOf<Live2DModelLifecycleManager?>(null) }
    var lifecycleStateName by remember(modelKey) { mutableStateOf<String?>(null) }
    var resetCounter by remember(modelKey) { mutableStateOf(0) }

    val cropLauncher =
            rememberLauncherForActivityResult(StartActivityForResult()) { result ->
                if (result.resultCode == Activity.RESULT_OK) {
                    val output = result.data?.let { UCrop.getOutput(it) }
                    if (output != null) {
                        scope.launch {
                            val copied = copyImageToInternal(context, output)
                            if (copied != null) {
                                wallpaperTempPath = copied
                            } else {
                                uiLogger.error("裁剪后的壁纸复制失败")
                                wallpaperTempPath = ""
                            }
                            deleteTempUri(context, output)
                        }
                    }
                } else if (result.resultCode == UCrop.RESULT_ERROR) {
                    val error = result.data?.let { UCrop.getError(it) }
                    uiLogger.error("裁剪失败", error)
                }
            }

    val imagePickerLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
                if (uri != null) {
                    val destFile =
                            File(
                                    context.cacheDir,
                                    "wallpaper_crop_${System.currentTimeMillis()}.jpg"
                            )
                    val authority = "${context.packageName}.fileprovider"
                    val destUri = FileProvider.getUriForFile(context, authority, destFile)
                    val options =
                            UCrop.Options().apply {
                                setCompressionFormat(Bitmap.CompressFormat.JPEG)
                                setCompressionQuality(95)
                                setHideBottomControls(false)
                                setFreeStyleCropEnabled(true)
                            }
                    try {
                        val metrics = context.resources.displayMetrics
                        val aspectX = metrics.widthPixels.coerceAtLeast(1)
                        val aspectY = metrics.heightPixels.coerceAtLeast(1)
                        val intent =
                                UCrop.of(uri, destUri)
                                        .withOptions(options)
                                        .withAspectRatio(aspectX.toFloat(), aspectY.toFloat())
                                        .withMaxResultSize(2048, 2048)
                                        .getIntent(context)
                                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                                        .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                        grantUriToCropApp(context, destUri)
                        cropLauncher.launch(intent)
                    } catch (e: Exception) {
                        uiLogger.error("启动裁剪失败", e)
                        context.deleteTempCacheFile(destFile)
                    }
                }
            }

    val agentProfileExportLauncher =
            rememberLauncherForActivityResult(
                    ActivityResultContracts.CreateDocument("application/json")
            ) { uri ->
                val json = pendingAgentProfileExportJson
                pendingAgentProfileExportJson = null
                if (uri != null && json != null) {
                    scope.launch {
                        val saved = writeTextToUri(context, uri, json)
                        Toast.makeText(
                                        context,
                                        if (saved) "角色配置已导出" else "角色配置导出失败",
                                        Toast.LENGTH_SHORT
                                )
                                .show()
                    }
                }
            }

    val agentProfileImportLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
                if (uri != null) {
                    scope.launch {
                        val json = readTextFromUri(context, uri)
                        if (json == null) {
                            Toast.makeText(context, "角色配置导入失败", Toast.LENGTH_SHORT).show()
                        } else {
                            agentProfileImportEvent =
                                    AgentProfileImportEvent(System.nanoTime(), json)
                        }
                    }
                }
            }

    // ---- Phase-7 life capture (photo/album/video/voice) ----
    var pendingPhotoFile by remember { mutableStateOf<File?>(null) }
    var pendingVideoFile by remember { mutableStateOf<File?>(null) }
    var isRecordingVoice by remember { mutableStateOf(false) }
    var activeVoiceRecorder by remember { mutableStateOf<MediaRecorder?>(null) }
    var activeVoiceFile by remember { mutableStateOf<File?>(null) }

    fun stopVoiceRecording(save: Boolean) {
        val recorder = activeVoiceRecorder
        val file = activeVoiceFile
        activeVoiceRecorder = null
        activeVoiceFile = null
        isRecordingVoice = false
        if (recorder != null) {
            // stop() throws RuntimeException when the recorder never actually started producing
            // frames (e.g. stop tapped immediately); that still means "discard".
            runCatching { recorder.stop() }
                    .onFailure { uiLogger.warn("停止录音失败", it) }
            recorder.release()
        }
        if (file == null) return
        if (save && file.exists() && file.length() > 0L) {
            chatManager.sendLifeCapture(file.absolutePath, "voice", inputText)
            inputText = ""
        } else {
            file.delete()
            if (save) Toast.makeText(context, "录音无效，已丢弃", Toast.LENGTH_SHORT).show()
        }
    }

    fun startVoiceRecording() {
        val file = LifeCaptureStore.newCaptureFile(context, "m4a")
        val recorder =
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) MediaRecorder(context)
                else @Suppress("DEPRECATION") MediaRecorder()
        try {
            recorder.setAudioSource(MediaRecorder.AudioSource.MIC)
            recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            recorder.setOutputFile(file.absolutePath)
            recorder.prepare()
            recorder.start()
            activeVoiceRecorder = recorder
            activeVoiceFile = file
            isRecordingVoice = true
        } catch (e: Exception) {
            uiLogger.error("启动录音失败", e)
            runCatching { recorder.release() }
            file.delete()
            Toast.makeText(context, "无法开始录音", Toast.LENGTH_SHORT).show()
        }
    }

    val takePictureLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { success ->
                val file = pendingPhotoFile
                pendingPhotoFile = null
                if (success && file != null && file.exists() && file.length() > 0L) {
                    chatManager.sendLifeCapture(file.absolutePath, "photo", inputText)
                    inputText = ""
                } else {
                    file?.delete()
                    Toast.makeText(context, "已取消拍照", Toast.LENGTH_SHORT).show()
                }
            }

    fun launchTakePicture() {
        val file = LifeCaptureStore.newCaptureFile(context, "jpg")
        val authority = "${context.packageName}.fileprovider"
        pendingPhotoFile = file
        takePictureLauncher.launch(FileProvider.getUriForFile(context, authority, file))
    }

    val cameraPermissionLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
                    granted ->
                if (granted) {
                    launchTakePicture()
                } else {
                    Toast.makeText(context, "需要相机权限才能拍照", Toast.LENGTH_SHORT).show()
                }
            }

    val albumPickerLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
                if (uri == null) return@rememberLauncherForActivityResult
                val caption = inputText
                scope.launch {
                    val path =
                            withContext(Dispatchers.IO) {
                                LifeCaptureStore.copyIntoStore(context, uri, "jpg")
                            }
                    if (path != null) {
                        chatManager.sendLifeCapture(path, "photo", caption)
                        inputText = ""
                    } else {
                        Toast.makeText(context, "图片保存失败", Toast.LENGTH_SHORT).show()
                    }
                }
            }

    val captureVideoLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.CaptureVideo()) { success ->
                val file = pendingVideoFile
                pendingVideoFile = null
                if (success && file != null && file.exists() && file.length() > 0L) {
                    chatManager.sendLifeCapture(file.absolutePath, "video", inputText)
                    inputText = ""
                } else {
                    file?.delete()
                    Toast.makeText(context, "已取消录像", Toast.LENGTH_SHORT).show()
                }
            }

    fun launchCaptureVideo() {
        val file = LifeCaptureStore.newCaptureFile(context, "mp4")
        val authority = "${context.packageName}.fileprovider"
        pendingVideoFile = file
        captureVideoLauncher.launch(FileProvider.getUriForFile(context, authority, file))
    }

    val micPermissionLauncher =
            rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
                    granted ->
                if (granted) {
                    startVoiceRecording()
                } else {
                    Toast.makeText(context, "需要麦克风权限才能录音", Toast.LENGTH_SHORT).show()
                }
            }

    DisposableEffect(Unit) {
        onDispose {
            val recorder = activeVoiceRecorder
            if (recorder != null) {
                runCatching { recorder.stop() }
                        .onFailure { uiLogger.warn("界面销毁时停止录音失败", it) }
                recorder.release()
                activeVoiceFile?.delete()
            }
        }
    }

    LaunchedEffect(chatManager) {
        chatManager.errors.collect { raw ->
            val message = raw.trim().ifEmpty { "运行时出现未知错误" }
            val entry = ConnectionErrorBanner(System.nanoTime(), message)
            connectionErrorBanners.add(entry)
            if (connectionErrorBanners.size > 5) {
                connectionErrorBanners.removeAt(0)
            }
        }
    }

    LaunchedEffect(Unit) {
        prefs.getString("nickname", null)?.let {
            nickname = it
            if (it.isNotBlank()) chatManager.setUserProfile(it)
        }
        wallpaperPrefs.getString(WallpaperComm.PREF_WALLPAPER_BG_PATH, null)?.let {
            if (wallpaperBgPath != it) wallpaperBgPath = it
            if (wallpaperTempPath != it) wallpaperTempPath = it
        }
    }

    // 运行时随聊天界面自动启动：进入即触发一次（ensureBound + 启动），不再依赖手动操作。
    // 配置无效时运行时会进入 ERROR 态，由错误横幅/状态行呈现，用户可通过齿轮设置修正后即生效。
    LaunchedEffect(Unit) { chatManager.startLocalRuntime() }

    LaunchedEffect(wallpaperBgPath) {
        val newBitmap = wallpaperBgPath.takeIf { it.isNotBlank() }?.let { loadBackgroundBitmap(it) }
        val previous = backgroundBitmap
        backgroundBitmap = newBitmap
        if (previous != null && previous != newBitmap && !previous.isRecycled) {
            previous.recycle()
        }
    }

    LaunchedEffect(lifecycleManager, wallpaperBgPath) {
        lifecycleManager?.updateBackgroundTexture(wallpaperBgPath.takeIf { it.isNotBlank() })
    }

    LaunchedEffect(
            currentModel,
            lifecycleStateName,
            wallpaperBgPath,
            visualSurfaceWidthPx,
            visualSurfaceHeightPx
    ) {
        val model = currentModel
        val cleanBackgroundPath = wallpaperBgPath.takeIf { it.isNotBlank() }
        chatManager.updateEnvironmentState(
                modelKey = model?.folderPath,
                modelName = model?.name,
                modelFolderPath = model?.folderPath,
                modelFile = model?.modelFile,
                lifecycleState = lifecycleStateName,
                motionFiles = model?.motionFiles.orEmpty(),
                appVisible = true,
                backgroundPath = cleanBackgroundPath,
                hasBackgroundPath = true,
                visualSnapshotReference =
                        buildEnvironmentVisualSnapshotReference(
                                source = "android_app",
                                modelFolderPath = model?.folderPath,
                                modelName = model?.name,
                                lifecycleState = lifecycleStateName,
                                backgroundPath = cleanBackgroundPath,
                                width = visualSurfaceWidthPx,
                                height = visualSurfaceHeightPx
                        ),
                visualSnapshotMimeType = ENVIRONMENT_VISUAL_SNAPSHOT_MIME_TYPE,
                visualSnapshotWidth = visualSurfaceWidthPx.takeIf { it > 0 },
                visualSnapshotHeight = visualSurfaceHeightPx.takeIf { it > 0 },
                visualSnapshotCapturedAtMillis = System.currentTimeMillis()
        )
    }

    DisposableEffect(Unit) { onDispose { backgroundBitmap?.takeIf { !it.isRecycled }?.recycle() } }

    LaunchedEffect(currentModel) {
        // "更换模型" is the single source of truth (chat_prefs/selected_model_folder, persisted by
        // MainActivity.persistModelSelection). Here we only notify the running wallpaper to switch live.
        sendWallpaperModelBroadcast(context, currentModel?.folderPath)
    }

    LaunchedEffect(modelKey) {
        if (selectedModel == null) {
            isLoadingDefaultModel = true
            val storedFolder = prefs.getString(ChatPreferenceKeys.SELECTED_MODEL_FOLDER, null)
            scope.launch {
                try {
                    val models = Live2DModelManager.scanModels(context)
                    val preferred =
                            storedFolder?.let { folder ->
                                models.firstOrNull { it.folderPath == folder }
                            }
                    val fallback =
                            models.find {
                                it.folderPath.contains("hiyori", true) ||
                                        it.name.contains("hiyori", true)
                            }
                                    ?: models.firstOrNull()
                    val resolved = preferred ?: fallback
                    if (resolved != null) {
                        currentModel = resolved
                        onModelChanged(resolved)
                    }
                } catch (_: Exception) {} finally {
                    isLoadingDefaultModel = false
                }
            }
        } else {
            currentModel = selectedModel
            isLoadingDefaultModel = false
        }
    }

    LaunchedEffect(currentModel, modelKey) {
        if (currentModel != null) {
            isResetting = true
            try {
                lifecycleManager?.setInteractionCallback(null)
                lifecycleManager?.setMotionPlaybackCallback(null)
                lifecycleManager?.destroy()
                lifecycleManager = null
                lifecycleStateName = null
                resetCubismFramework()
                delay(200)
                val newManager = Live2DModelLifecycleManager.create(context, currentModel!!)
                newManager.setStateCallback(
                        object : Live2DModelLifecycleManager.StateCallback {
                            override fun onStateChanged(
                                    newState: Live2DModelLifecycleManager.LifecycleState,
                                    message: String?
                            ) {
                                scope.launch { lifecycleStateName = newState.name }
                            }
                            override fun onError(error: String, exception: Throwable?) {
                                scope.launch {
                                    lifecycleStateName = newManager.getCurrentState().name
                                }
                            }
                        }
                )
                newManager.setInteractionCallback(
                        object : Live2DModelLifecycleManager.InteractionCallback {
                            override fun onInteraction(
                                    type: String,
                                    x: Float?,
                                    y: Float?,
                                    timestampMillis: Long
                            ) {
                                chatManager.updateEnvironmentInteraction(
                                        type = type,
                                        x = x,
                                        y = y,
                                        timestampMillis = timestampMillis
                                )
                            }
                        }
                )
                newManager.setMotionPlaybackCallback(
                        object : Live2DModelLifecycleManager.MotionPlaybackCallback {
                            override fun onMotionFinished(
                                    group: String?,
                                    index: Int?,
                                    filePath: String?,
                                    loop: Boolean,
                                    timestampMillis: Long
                            ) {
                                chatManager.reportMotionFinished(
                                        group = group,
                                        index = index,
                                        filePath = filePath,
                                        loop = loop,
                                        timestampMillis = timestampMillis
                                )
                            }
                        }
                )
                if (newManager.initialize()) {
                    lifecycleManager = newManager
                    lifecycleStateName = newManager.getCurrentState().name
                    chatManager.setMotionCommandCallback { command ->
                        newManager.playMotionCommand(command)
                    }
                    chatManager.clearMessagesEphemeral()
                    chatManager.setActiveModel(currentModel?.name)
                }
            } catch (_: Exception) {} finally {
                isResetting = false
                resetCounter++
            }
        }
    }

    DisposableEffect(modelKey) {
        onDispose {
            lifecycleManager?.setInteractionCallback(null)
            lifecycleManager?.setMotionPlaybackCallback(null)
            lifecycleManager?.destroy()
            chatManager.updateEnvironmentState(
                    appVisible = false,
                    visualSnapshotReference = null,
                    hasVisualSnapshot = true
            )
        }
    }

    val chatInputHeightDp = with(LocalDensity.current) { chatInputHeightPx.toDp() }
    val floatingBottomPadding = maxOf(ReservedBottomHeight, chatInputHeightDp) + 8.dp

    Box(
            modifier =
                    Modifier.fillMaxSize()
                            .onSizeChanged { size ->
                                if (visualSurfaceWidthPx != size.width) {
                                    visualSurfaceWidthPx = size.width
                                }
                                if (visualSurfaceHeightPx != size.height) {
                                    visualSurfaceHeightPx = size.height
                                }
                            }
    ) {
        backgroundBitmap?.let { bmp ->
            Image(
                    bitmap = bmp.asImageBitmap(),
                    contentDescription = "聊天背景",
                    modifier = Modifier.fillMaxSize(),
                    contentScale = ContentScale.Crop
            )
        }
                ?: Box(modifier = Modifier.fillMaxSize().background(Color(0xFF101010)))

        if (isLoadingDefaultModel) {
            Column(
                    modifier = Modifier.align(Alignment.Center),
                    horizontalAlignment = Alignment.CenterHorizontally
            ) {
                CircularProgressIndicator()
                Spacer(modifier = Modifier.height(16.dp))
                Text("正在加载默认模型...")
            }
        } else if (isResetting) {
            Column(
                    modifier = Modifier.align(Alignment.Center),
                    horizontalAlignment = Alignment.CenterHorizontally
            ) {
                CircularProgressIndicator()
                Spacer(modifier = Modifier.height(16.dp))
                Text("正在重置模型...")
                Spacer(modifier = Modifier.height(8.dp))
                Text("正在清理资源并重新初始化", style = MaterialTheme.typography.bodySmall)
            }
        } else if (currentModel != null) {
            // 重新布局：模型始终全屏放在最底层；TopBar + 消息 + 输入框作为浮层，不再挤压 GLSurfaceView 尺寸
            Box(modifier = Modifier.fillMaxSize()) {
                // 使用 Column 保留底部固定高度空白区域，防止模型绘制与输入区重叠
                Column(modifier = Modifier.fillMaxSize()) {
                    Box(
                            modifier =
                                    Modifier.weight(1f)
                                            .fillMaxWidth()
                                            // 下移模型绘制区域，避免头像穿过顶部栏
                                            .padding(top = TopBarHeight)
                    ) {
                        Live2DModelViewer(
                                model = currentModel!!,
                                modelKey = resetCounter,
                                chatManager = chatManager,
                                lifecycleManager = lifecycleManager,
                                modifier = Modifier.fillMaxSize()
                        )
                    }
                    // 固定空白区域，不随输入框/键盘变化
                    Spacer(modifier = Modifier.fillMaxWidth().height(ReservedBottomHeight))
                }

                // 顶部栏浮层
                var overflowExpanded by remember { mutableStateOf(false) }
                TopAppBar(
                        title = {
                            Column {
                                Text(personaDisplayName)
                                Text(
                                        text = runtimeStatusLine,
                                        style = MaterialTheme.typography.bodySmall,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                )
                            }
                        },
                        actions = {
                            // 聊天记录浏览：进入全屏历史记录模式（默认界面只浮现最近几条）。
                            IconButton(onClick = { showHistoryViewer = true }) {
                                Icon(Icons.Default.History, contentDescription = "聊天记录")
                            }
                            IconButton(onClick = { applyLiveWallpaper(context) }) {
                                Icon(Icons.Default.Wallpaper, contentDescription = "应用为系统壁纸")
                            }
                            IconButton(
                                    onClick = {
                                        wallpaperTempPath = wallpaperBgPath
                                        showWallpaperDialog = true
                                    }
                            ) { Icon(Icons.Default.Image, contentDescription = "壁纸背景设置") }
                            // 配置按钮（运行时随界面自动启动；改完配置即重启生效）
                            IconButton(onClick = { showConnectionDialog = true }) {
                                Icon(Icons.Default.Settings, contentDescription = "运行设置")
                            }
                            Box {
                                IconButton(onClick = { overflowExpanded = true }) {
                                    Icon(Icons.Default.MoreVert, contentDescription = "更多操作")
                                }
                                DropdownMenu(
                                        expanded = overflowExpanded,
                                        onDismissRequest = { overflowExpanded = false }
                                ) {
                                    DropdownMenuItem(
                                            text = { Text("角色配置") },
                                            onClick = {
                                                overflowExpanded = false
                                                showAgentProfileDialog = true
                                            }
                                    )
                                    DropdownMenuItem(
                                            text = { Text("查看日志") },
                                            onClick = {
                                                overflowExpanded = false
                                                showLogViewer = true
                                                uiLogger.info(
                                                        "Log viewer opened from overflow menu"
                                                )
                                            }
                                    )
                                    DropdownMenuItem(
                                            text = { Text("更换模型") },
                                            onClick = {
                                                overflowExpanded = false
                                                onModelSelectionRequest()
                                            }
                                    )
                                    DropdownMenuItem(
                                            text = { Text("切换人设") },
                                            onClick = {
                                                overflowExpanded = false
                                                showPersonaDialog = true
                                            }
                                    )
                                    DropdownMenuItem(
                                            text = { Text("Worker 配置") },
                                            onClick = {
                                                overflowExpanded = false
                                                showWorkerConfigDialog = true
                                            }
                                    )
                                    DropdownMenuItem(
                                            text = { Text("清空聊天记录") },
                                            onClick = {
                                                overflowExpanded = false
                                                chatManager.clearMessages()
                                            }
                                    )
                                }
                            }
                        },
                        modifier = Modifier.align(Alignment.TopCenter)
                )

                if (connectionErrorBanners.isNotEmpty()) {
                    Column(
                            modifier =
                                    Modifier.align(Alignment.TopStart)
                                            .padding(
                                                    start = 12.dp,
                                                    top = TopBarHeight + 12.dp,
                                                    end = 12.dp
                                            )
                                            .widthIn(max = 360.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        connectionErrorBanners.forEach { banner ->
                            key(banner.id) {
                                LaunchedEffect(banner.id) {
                                    delay(6_000)
                                    connectionErrorBanners.remove(banner)
                                }
                                ConnectionErrorToast(
                                        message = banner.message,
                                        onDismiss = { connectionErrorBanners.remove(banner) }
                                )
                            }
                        }
                    }
                }

                FloatingMessagesOverlay(
                        recentMessages = messages,
                        assistantName = personaDisplayName,
                        userNickname = currentUserNickname,
                        isThinking = isThinking,
                        modifier =
                                Modifier.align(Alignment.BottomStart)
                                        .padding(start = 12.dp, bottom = floatingBottomPadding)
                )

                ChatInputBar(
                        inputText = inputText,
                        onInputChange = { inputText = it },
                        onSend = {
                            if (inputText.isNotBlank()) {
                                chatManager.sendUserMessage(inputText.trim())
                                inputText = ""
                            }
                        },
                        isRecordingVoice = isRecordingVoice,
                        onCapturePhoto = {
                            if (ContextCompat.checkSelfPermission(
                                            context,
                                            android.Manifest.permission.CAMERA
                                    ) == PackageManager.PERMISSION_GRANTED
                            ) {
                                launchTakePicture()
                            } else {
                                cameraPermissionLauncher.launch(
                                        android.Manifest.permission.CAMERA
                                )
                            }
                        },
                        onPickAlbum = { albumPickerLauncher.launch("image/*") },
                        onCaptureVideo = { launchCaptureVideo() },
                        onToggleVoice = {
                            if (isRecordingVoice) {
                                stopVoiceRecording(save = true)
                            } else if (ContextCompat.checkSelfPermission(
                                            context,
                                            android.Manifest.permission.RECORD_AUDIO
                                    ) == PackageManager.PERMISSION_GRANTED
                            ) {
                                startVoiceRecording()
                            } else {
                                micPermissionLauncher.launch(
                                        android.Manifest.permission.RECORD_AUDIO
                                )
                            }
                        },
                        modifier =
                                Modifier.align(Alignment.BottomCenter).onSizeChanged { coords ->
                                    val newHeight = coords.height
                                    if (chatInputHeightPx != newHeight) {
                                        chatInputHeightPx = newHeight
                                    }
                                }
                )
            }
        } else {
            Column(
                    modifier = Modifier.align(Alignment.Center),
                    horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Icon(
                        Icons.Default.Warning,
                        contentDescription = null,
                        modifier = Modifier.size(64.dp),
                        tint = MaterialTheme.colorScheme.error
                )
                Spacer(modifier = Modifier.height(16.dp))
                Text("没有找到可用的Live2D模型")
                Spacer(modifier = Modifier.height(8.dp))
                Button(onClick = onModelSelectionRequest) { Text("选择模型") }
            }
        }
        if (showConnectionDialog) {
            ConnectionConfigDialog(
                    nickname = nickname,
                    localLlmSettings = effectiveLocalLlmSettings,
                    onNicknameChange = { nickname = it },
                    onSave = { valid, nextLocalLlmSettings ->
                        if (valid) {
                            chatManager.setUserProfile(nickname)
                            prefs.edit().putString("nickname", nickname).apply()
                            chatManager.updateLocalLlmSettings(nextLocalLlmSettings)
                            chatManager.startLocalRuntime()
                            showConnectionDialog = false
                        }
                    },
                    onDismiss = { showConnectionDialog = false }
            )
        }
        if (showWorkerConfigDialog) {
            WorkerConfigDialog(
                    settings = workerLlmSettings,
                    onSave = { next ->
                        chatManager.updateWorkerLlmSettings(next)
                        showWorkerConfigDialog = false
                    },
                    onDismiss = { showWorkerConfigDialog = false }
            )
        }
        if (showPersonaDialog) {
            AlertDialog(
                    onDismissRequest = { showPersonaDialog = false },
                    title = { Text("切换人设") },
                    text = {
                        Column {
                            PersonaRegistry.availablePersonas().forEach { persona ->
                                val pick = {
                                    if (persona.id != selectedPersona) {
                                        chatManager.setActivePersona(persona.id)
                                    }
                                    showPersonaDialog = false
                                }
                                Row(
                                        modifier =
                                                Modifier.fillMaxWidth()
                                                        .clickable(onClick = pick)
                                                        .padding(vertical = 12.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                ) {
                                    RadioButton(
                                            selected = persona.id == selectedPersona,
                                            onClick = pick
                                    )
                                    Spacer(Modifier.width(8.dp))
                                    Text(persona.displayName)
                                }
                            }
                        }
                    },
                    confirmButton = {
                        TextButton(onClick = { showPersonaDialog = false }) { Text("取消") }
                    }
            )
        }
        if (showAgentProfileDialog) {
            AgentProfileConfigDialog(
                    modelName = currentModel?.name,
                    importEvent = agentProfileImportEvent,
                    onImportEventConsumed = { event ->
                        if (agentProfileImportEvent?.id == event.id) {
                            agentProfileImportEvent = null
                        }
                    },
                    onImportRequest = {
                        runCatching {
                                    agentProfileImportLauncher.launch(
                                            arrayOf("application/json", "text/*", "*/*")
                                    )
                                }
                                .onFailure { error ->
                                    uiLogger.warn("启动角色配置导入失败", error)
                                    Toast.makeText(context, "无法打开文件选择器", Toast.LENGTH_SHORT)
                                            .show()
                                }
                    },
                    onExportJson = { fileName, json ->
                        pendingAgentProfileExportJson = json
                        runCatching { agentProfileExportLauncher.launch(fileName) }
                                .onFailure { error ->
                                    pendingAgentProfileExportJson = null
                                    uiLogger.warn("启动角色配置导出失败", error)
                                    Toast.makeText(context, "无法打开文件保存器", Toast.LENGTH_SHORT)
                                            .show()
                                }
                    },
                    onSaved = { chatManager.startLocalRuntime() },
                    onDismiss = { showAgentProfileDialog = false }
            )
        }
        if (showLogViewer) {
            LogViewerDialog(onDismiss = { showLogViewer = false })
        }
        if (showHistoryViewer) {
            ChatHistoryViewer(
                    messages = messages,
                    userNickname = currentUserNickname,
                    assistantName = personaDisplayName,
                    onClose = { showHistoryViewer = false },
            )
        }
        if (showWallpaperDialog) {
            WallpaperSettingsDialog(
                    currentPath = wallpaperBgPath,
                    tempPath = wallpaperTempPath,
                    bubbleCount = wallpaperBubbleCount,
                    onBubbleCountChange = { wallpaperBubbleCount = it },
                    onPickImage = { imagePickerLauncher.launch("image/*") },
                    onClearImage = { wallpaperTempPath = "" },
                    onApply = {
                        val finalPath = wallpaperTempPath.ifBlank { null }
                        wallpaperBgPath = finalPath.orEmpty()
                        wallpaperTempPath = wallpaperBgPath
                        val clampedCount =
                                wallpaperBubbleCount.coerceIn(
                                        WallpaperComm.MIN_BUBBLE_COUNT,
                                        WallpaperComm.MAX_BUBBLE_COUNT
                                )
                        wallpaperBubbleCount = clampedCount
                        val saveSucceeded =
                                wallpaperPrefs
                                        .edit()
                                        .apply {
                                            if (finalPath != null) {
                                                putString(
                                                        WallpaperComm.PREF_WALLPAPER_BG_PATH,
                                                        finalPath
                                                )
                                            } else {
                                                remove(WallpaperComm.PREF_WALLPAPER_BG_PATH)
                                            }
                                            putInt(
                                                    WallpaperComm.PREF_WALLPAPER_BUBBLE_COUNT,
                                                    clampedCount
                                            )
                                        }
                                        .commit()
                        uiLogger.debug(
                                "保存壁纸路径${if (saveSucceeded) "成功" else "失败"}: ${finalPath ?: "(清除)"}，气泡条数=$clampedCount"
                        )
                        lifecycleManager?.updateBackgroundTexture(finalPath)
                        sendWallpaperRefreshBroadcast(context, finalPath)
                        sendWallpaperBubbleCountBroadcast(context, clampedCount)
                        showWallpaperDialog = false
                    },
                    onDismiss = {
                        wallpaperTempPath = wallpaperBgPath
                        showWallpaperDialog = false
                    }
            )
        }
    }
}

private fun Live2DModelLifecycleManager.playMotionCommand(command: MotionCommand): Boolean {
    val group = command.group
    val index = command.index
    return if (group != null && index != null) {
        playMotionByGroup(group, index, command.loop)
    } else {
        val filePath = command.filePath ?: return false
        playMotionByFile(filePath, command.loop)
    }
}

@Composable
private fun ChatInputBar(
        inputText: String,
        onInputChange: (String) -> Unit,
        onSend: () -> Unit,
        isRecordingVoice: Boolean,
        onCapturePhoto: () -> Unit,
        onPickAlbum: () -> Unit,
        onCaptureVideo: () -> Unit,
        onToggleVoice: () -> Unit,
        modifier: Modifier = Modifier
) {
    Surface(
            tonalElevation = 3.dp,
            shadowElevation = 8.dp,
            color = MaterialTheme.colorScheme.surface.copy(alpha = 0.92f),
            modifier = modifier.fillMaxWidth().imePadding().navigationBarsPadding()
    ) {
        Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.Bottom
        ) {
            var attachExpanded by remember { mutableStateOf(false) }
            Box {
                IconButton(
                        onClick = {
                            if (isRecordingVoice) onToggleVoice() else attachExpanded = true
                        }
                ) {
                    Icon(
                            imageVector =
                                    if (isRecordingVoice) Icons.Default.Stop
                                    else Icons.Default.Add,
                            contentDescription =
                                    if (isRecordingVoice) "停止录音" else "添加附件",
                            tint =
                                    if (isRecordingVoice) MaterialTheme.colorScheme.error
                                    else LocalContentColor.current
                    )
                }
                DropdownMenu(
                        expanded = attachExpanded,
                        onDismissRequest = { attachExpanded = false }
                ) {
                    DropdownMenuItem(
                            text = { Text("拍照") },
                            onClick = {
                                attachExpanded = false
                                onCapturePhoto()
                            }
                    )
                    DropdownMenuItem(
                            text = { Text("相册") },
                            onClick = {
                                attachExpanded = false
                                onPickAlbum()
                            }
                    )
                    DropdownMenuItem(
                            text = { Text("视频") },
                            onClick = {
                                attachExpanded = false
                                onCaptureVideo()
                            }
                    )
                    DropdownMenuItem(
                            text = { Text("语音") },
                            onClick = {
                                attachExpanded = false
                                onToggleVoice()
                            }
                    )
                }
            }
            Spacer(modifier = Modifier.width(4.dp))
            OutlinedTextField(
                    value = inputText,
                    onValueChange = onInputChange,
                    modifier = Modifier.weight(1f),
                    placeholder = {
                        Text(if (isRecordingVoice) "正在录音，点左侧停止..." else "输入消息...")
                    },
                    maxLines = 4
            )
            Spacer(modifier = Modifier.width(8.dp))
            FloatingActionButton(
                    onClick = onSend,
                    modifier = Modifier.size(48.dp),
                    containerColor = MaterialTheme.colorScheme.primary
            ) { Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送") }
        }
    }
}

private suspend fun resetCubismFramework() {
    try {
        ImprovedLive2DRenderer.safeShutdownFramework()
        delay(100)
        ImprovedLive2DRenderer.ensureFrameworkInitialized()
    } catch (_: Exception) {}
}

@Composable
@Suppress("UNUSED_PARAMETER")
fun Live2DModelViewer(
        model: Live2DModelManager.ModelInfo,
        modelKey: Int,
        chatManager: ChatServiceClient,
        lifecycleManager: Live2DModelLifecycleManager?,
        modifier: Modifier = Modifier
) {
    var errorMessage by remember(modelKey) { mutableStateOf<String?>(null) }
    Box(modifier = modifier) {
        if (lifecycleManager != null && errorMessage == null) {
            val glSurfaceView = remember(modelKey) { lifecycleManager.createGLSurfaceView() }
            if (glSurfaceView != null) {
                AndroidView(
                        factory = { glSurfaceView },
                        modifier = Modifier.fillMaxSize(),
                        update = { lifecycleManager.startRendering() }
                )
            } else {
                Column(
                        modifier = Modifier.align(Alignment.Center),
                        horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Icon(
                            Icons.Default.Warning,
                            contentDescription = null,
                            modifier = Modifier.size(48.dp),
                            tint = MaterialTheme.colorScheme.error
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("无法创建渲染视图")
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("Key: $modelKey", style = MaterialTheme.typography.bodySmall)
                }
            }
        } else if (errorMessage != null) {
            Column(
                    modifier = Modifier.align(Alignment.Center),
                    horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Icon(
                        Icons.Default.Warning,
                        contentDescription = null,
                        modifier = Modifier.size(48.dp),
                        tint = MaterialTheme.colorScheme.error
                )
                Spacer(modifier = Modifier.height(8.dp))
                Text(text = errorMessage!!, color = MaterialTheme.colorScheme.error)
            }
        } else {
            Column(
                    modifier = Modifier.align(Alignment.Center),
                    horizontalAlignment = Alignment.CenterHorizontally
            ) {
                CircularProgressIndicator()
                Spacer(modifier = Modifier.height(16.dp))
                Text("正在加载模型...")
                Spacer(modifier = Modifier.height(8.dp))
                Text("Key: $modelKey", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun ConnectionErrorToast(message: String, onDismiss: () -> Unit) {
    Surface(
            color = MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.96f),
            contentColor = MaterialTheme.colorScheme.onErrorContainer,
            tonalElevation = 6.dp,
            shadowElevation = 8.dp,
            shape = RoundedCornerShape(12.dp)
    ) {
        Row(
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Icon(
                    imageVector = Icons.Default.Warning,
                    contentDescription = null,
                    modifier = Modifier.size(20.dp)
            )
            Text(
                    text = message,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.weight(1f, fill = false)
            )
            IconButton(onClick = onDismiss) {
                Icon(Icons.Default.Close, contentDescription = "关闭错误提示")
            }
        }
    }
}

@Composable
private fun FloatingMessagesOverlay(
        recentMessages: List<ChatServiceClient.ChatMessageSnapshot>,
        assistantName: String,
        userNickname: String?,
        isThinking: Boolean = false,
        modifier: Modifier = Modifier
) {
    val scope = rememberCoroutineScope()
    val tail = remember(recentMessages) { recentMessages.takeLast(5) }
    val hiddenMap = remember { mutableStateMapOf<String, Boolean>() }
    // A reply that grows past 3/4 of the screen would cover the input/controls and trap the UI, so
    // hide such an oversized bubble from the floating overlay (it stays fully readable in 查看日志).
    val screenHeightDp = LocalConfiguration.current.screenHeightDp
    val maxBubbleHeightPx = with(LocalDensity.current) { (screenHeightDp.dp * 0.75f).toPx() }
    val fadingSet = remember { mutableStateMapOf<String, Boolean>() }
    Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        tail.forEachIndexed { index, msg ->
            if ((hiddenMap[msg.id] ?: false)) return@forEachIndexed
            // An AI-agent activity message renders INLINE as its status/summary bubble (left-aligned),
            // sitting in the conversation flow at the point the agent was called.
            val agentJson = msg.agentActivityJson
            if (agentJson != null) {
                Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.Start
                ) { WorkerStatusBubble(agentJson) }
                return@forEachIndexed
            }
            val fileJson = msg.fileInfoJson
            if (fileJson != null) {
                FileBubble(fileJson)
                return@forEachIndexed
            }
            val animAlpha = remember(msg.id) { Animatable(1f) }
            val shouldFadeOut = tail.size >= 5 && index == 0
            Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement =
                            if (msg.isFromUser) Arrangement.End else Arrangement.Start
            ) {
                Card(
                        colors =
                                CardDefaults.cardColors(
                                        containerColor =
                                                MaterialTheme.colorScheme.surface.copy(alpha = 0.9f)
                                ),
                        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
                        modifier =
                                Modifier.alpha(animAlpha.value).onSizeChanged { size ->
                                    if (size.height > maxBubbleHeightPx) hiddenMap[msg.id] = true
                                }
                ) {
                    Column(modifier = Modifier.padding(10.dp).widthIn(max = 320.dp)) {
                        val title =
                                if (msg.isFromUser) (userNickname ?: "我")
                                else assistantName
                        Text(
                                text = title,
                                style = MaterialTheme.typography.labelMedium,
                                color =
                                        if (msg.isFromUser) MaterialTheme.colorScheme.primary
                                        else MaterialTheme.colorScheme.secondary
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            SelectionContainer(modifier = Modifier.weight(1f, fill = false)) {
                                Text(text = msg.content)
                            }
                            if (msg.failed) {
                                Spacer(modifier = Modifier.width(6.dp))
                                Icon(
                                        imageVector = Icons.Default.Warning,
                                        contentDescription = "发送失败：网络错误，消息未送达",
                                        tint = MaterialTheme.colorScheme.error,
                                        modifier = Modifier.size(16.dp)
                                )
                            }
                        }
                        if (msg.failed) {
                            Text(
                                    text = "发送失败，请检查网络后重试",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.error
                            )
                        }
                    }
                }
                LaunchedEffect(shouldFadeOut) {
                    if (shouldFadeOut && fadingSet[msg.id] != true) {
                        fadingSet[msg.id] = true
                        scope.launch {
                            animAlpha.animateTo(0f, animationSpec = tween(durationMillis = 2000))
                            hiddenMap[msg.id] = true
                        }
                    }
                }
            }
        }

        // The live "thinking" indicator (agent activity is now an inline conversation bubble above).
        if (isThinking) {
            ThinkingBubble()
        }
    }
}

@Composable
private fun WorkerStatusBubble(status: String) {
    val context = LocalContext.current
    val activity = remember(status) { WorkerActivity.parse(status) } ?: return
    val done = activity.isDone
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
        Card(
                colors =
                        CardDefaults.cardColors(
                                containerColor =
                                        MaterialTheme.colorScheme.primaryContainer.copy(
                                                alpha = if (done) 0.85f else 0.92f
                                        )
                        ),
                elevation = CardDefaults.cardElevation(defaultElevation = 2.dp)
        ) {
            Row(
                    modifier =
                            Modifier.padding(horizontal = 12.dp, vertical = 8.dp)
                                    .widthIn(max = 320.dp),
                    verticalAlignment = Alignment.CenterVertically
            ) {
                Text(text = if (done) "✅" else "🔧", style = MaterialTheme.typography.bodyMedium)
                Spacer(modifier = Modifier.width(8.dp))
                Column(modifier = Modifier.weight(1f, fill = false)) {
                    Text(
                            text = if (done) "AI 智能体调用完成" else "AI 智能体工作中",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                    Text(
                            text =
                                    if (done) "${activity.steps} 步 · ${activity.tools} 次工具调用"
                                    else activity.text.take(100),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.85f),
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis
                    )
                }
                // Browser icon: only when the agent used the browser. Tapping it asks the (hidden)
                // overlay to expand so the user can view the current tab / live page.
                if (activity.browser) {
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(
                            text = "🌐",
                            style = MaterialTheme.typography.titleMedium,
                            modifier =
                                    Modifier.clip(CircleShape)
                                            .clickable { BrowserComm.requestExpand(context) }
                                            .padding(6.dp)
                    )
                }
            }
        }
    }
}

/**
 * Full-screen chat-history browsing mode. The default chat surface only floats the last few messages
 * over the character; this lets the user scroll the full loaded conversation (newest at the bottom).
 */
@Composable
private fun ChatHistoryViewer(
        messages: List<ChatServiceClient.ChatMessageSnapshot>,
        userNickname: String?,
        assistantName: String,
        onClose: () -> Unit,
) {
    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.fillMaxSize().statusBarsPadding()) {
            Row(
                    modifier =
                            Modifier.fillMaxWidth()
                                    .background(MaterialTheme.colorScheme.surface)
                                    .padding(horizontal = 4.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = onClose) {
                    Icon(Icons.Default.Close, contentDescription = "关闭")
                }
                Text(
                        text = "聊天记录（${messages.size}）",
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.weight(1f),
                )
            }
            if (messages.isEmpty()) {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("暂无聊天记录", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            } else {
                val listState = rememberLazyListState()
                LaunchedEffect(messages.size) {
                    listState.scrollToItem((messages.size - 1).coerceAtLeast(0))
                }
                LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize().padding(horizontal = 12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    item { Spacer(modifier = Modifier.height(8.dp)) }
                    items(messages, key = { it.id }) { msg ->
                        val agentJson = msg.agentActivityJson
                        val fileJson = msg.fileInfoJson
                        if (agentJson != null) {
                            Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.Start
                            ) { WorkerStatusBubble(agentJson) }
                        } else if (fileJson != null) {
                            FileBubble(fileJson)
                        } else {
                            HistoryBubble(msg, userNickname, assistantName)
                        }
                    }
                    item { Spacer(modifier = Modifier.height(12.dp)) }
                }
            }
        }
    }
}

@Composable
private fun HistoryBubble(
        msg: ChatServiceClient.ChatMessageSnapshot,
        userNickname: String?,
        assistantName: String,
) {
    val fromUser = msg.isFromUser
    Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = if (fromUser) Arrangement.End else Arrangement.Start,
    ) {
        Card(
                colors =
                        CardDefaults.cardColors(
                                containerColor =
                                        if (fromUser) MaterialTheme.colorScheme.primaryContainer
                                        else MaterialTheme.colorScheme.surfaceVariant
                        ),
                modifier = Modifier.widthIn(max = 300.dp),
        ) {
            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
                Text(
                        text =
                                if (fromUser) userNickname?.takeIf { it.isNotBlank() } ?: "我"
                                else assistantName,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary,
                )
                SelectionContainer {
                    Text(
                            text = msg.content,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurface,
                    )
                }
                if (msg.timestamp > 0L) {
                    Text(
                            text = formatHistoryTime(msg.timestamp),
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f),
                    )
                }
            }
        }
    }
}

private fun formatHistoryTime(ts: Long): String =
        java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.getDefault())
                .format(java.util.Date(ts))

/** A tappable attachment bubble for a file the worker submitted (`submit_file`). */
@Composable
private fun FileBubble(infoJson: String) {
    val context = LocalContext.current
    val info =
            remember(infoJson) {
                runCatching { com.google.gson.JsonParser.parseString(infoJson).asJsonObject }
                        .getOrNull()
            }
                    ?: return
    fun str(k: String): String? =
            info.get(k)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asString
    val name = str("name") ?: "file"
    val path = str("path")
    val mime = str("mime")
    val size = info.get("size")?.takeIf { it.isJsonPrimitive }?.asLong ?: 0L
    val description = str("description")
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
        Card(
                colors =
                        CardDefaults.cardColors(
                                containerColor =
                                        MaterialTheme.colorScheme.secondaryContainer.copy(
                                                alpha = 0.92f
                                        )
                        ),
                elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
                modifier = Modifier.clickable { openSubmittedFile(context, path, mime) }
        ) {
            Row(
                    modifier = Modifier.padding(12.dp).widthIn(max = 320.dp),
                    verticalAlignment = Alignment.CenterVertically
            ) {
                Text("📎", style = MaterialTheme.typography.titleMedium)
                Spacer(modifier = Modifier.width(10.dp))
                Column {
                    Text(name, style = MaterialTheme.typography.bodyMedium)
                    val sub = buildString {
                        append(formatFileSize(size))
                        if (!description.isNullOrBlank()) {
                            append(" · ")
                            append(description)
                        } else {
                            append(" · 点按打开")
                        }
                    }
                    Text(
                            sub,
                            style = MaterialTheme.typography.labelSmall,
                            color =
                                    MaterialTheme.colorScheme.onSecondaryContainer.copy(
                                            alpha = 0.7f
                                    )
                    )
                }
            }
        }
    }
}

private fun formatFileSize(bytes: Long): String =
        when {
            bytes >= 1024 * 1024 -> "%.1f MB".format(bytes / (1024.0 * 1024))
            bytes >= 1024 -> "%.1f KB".format(bytes / 1024.0)
            else -> "$bytes B"
        }

private fun openSubmittedFile(context: android.content.Context, path: String?, mime: String?) {
    if (path.isNullOrBlank()) return
    runCatching {
        val uri =
                androidx.core.content.FileProvider.getUriForFile(
                        context,
                        "${context.packageName}.fileprovider",
                        java.io.File(path)
                )
        context.startActivity(
                android.content.Intent(android.content.Intent.ACTION_VIEW).apply {
                    setDataAndType(uri, mime ?: "*/*")
                    addFlags(
                            android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION or
                                    android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                    )
                }
        )
    }
}

@Composable
private fun ThinkingBubble() {
    val transition = rememberInfiniteTransition(label = "thinking")
    val alpha by
            transition.animateFloat(
                    initialValue = 0.35f,
                    targetValue = 1f,
                    animationSpec =
                            infiniteRepeatable(
                                    animation = tween(durationMillis = 700),
                                    repeatMode = RepeatMode.Reverse
                            ),
                    label = "thinking_alpha"
            )
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
        Card(
                colors =
                        CardDefaults.cardColors(
                                containerColor =
                                        MaterialTheme.colorScheme.surface.copy(alpha = 0.9f)
                        ),
                elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
                modifier = Modifier.alpha(alpha)
        ) {
            Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                        text = "正在思考",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.secondary
                )
                Spacer(modifier = Modifier.width(6.dp))
                Text(
                        text = "•••",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.secondary
                )
            }
        }
    }
}

private fun applyLiveWallpaper(context: Context) {
    val component = ComponentName(context, Live2DWallpaperService::class.java)
    val activity = context.findActivity()

    fun launch(intent: Intent): Boolean {
        return try {
            if (activity != null) {
                activity.startActivity(intent)
            } else {
                context.startActivity(Intent(intent).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            true
        } catch (t: Throwable) {
            uiLogger.warn("启动壁纸意图失败: ${intent.action}", t)
            false
        }
    }

    val pm = context.packageManager
    val directIntent =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN) {
                Intent(WallpaperManager.ACTION_CHANGE_LIVE_WALLPAPER).apply {
                    putExtra(WallpaperManager.EXTRA_LIVE_WALLPAPER_COMPONENT, component)
                }
            } else {
                null
            }

    val launchedDirect =
            directIntent?.takeIf { it.resolveActivity(pm) != null }?.let { launch(it) } ?: false

    if (!launchedDirect) {
        val chooserIntent =
                Intent(WallpaperManager.ACTION_LIVE_WALLPAPER_CHOOSER).apply {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN) {
                        putExtra(WallpaperManager.EXTRA_LIVE_WALLPAPER_COMPONENT, component)
                    }
                }
        if (!launch(chooserIntent)) {
            Toast.makeText(context, "无法打开系统壁纸设置，请前往系统设置手动选择", Toast.LENGTH_LONG).show()
        }
    }
}

private fun validateConfig(
        localLlmSettings: LocalLlmSettings = LocalLlmSettings()
): List<String> = RuntimeSettingsValidation.validateConfig(localLlmSettings)

private fun buildRuntimeStatusLine(
        runtimeState: ChatServiceClient.RuntimeState,
        runtimeLabel: String,
        runtimeDiagnostic: String,
        localLlmSettings: LocalLlmSettings
): String {
    val stateLabel = runtimeLabel.ifBlank { fallbackRuntimeStatusLabel(runtimeState) }
    val baseLine = "${localProviderStatusText(localLlmSettings)} · $stateLabel"
    val diagnostic =
            runtimeDiagnostic.trim().takeIf {
                runtimeState == ChatServiceClient.RuntimeState.ERROR &&
                        it.isNotEmpty() &&
                        !baseLine.contains(it)
            }
    return diagnostic?.let { "$baseLine · $it" } ?: baseLine
}

private fun fallbackRuntimeStatusLabel(runtimeState: ChatServiceClient.RuntimeState): String =
        when (runtimeState) {
            ChatServiceClient.RuntimeState.STOPPED -> "本地运行时: stopped"
            ChatServiceClient.RuntimeState.STARTING -> "本地运行时: starting"
            ChatServiceClient.RuntimeState.RUNNING -> "本地运行时: ready"
            ChatServiceClient.RuntimeState.ERROR -> "本地运行时: error"
        }

private fun localProviderStatusText(settings: LocalLlmSettings): String {
    if (!settings.enabled) return "LLM Provider: 未启用"
    val endpoint = settings.baseUrl?.trim()?.takeIf { it.isNotEmpty() } ?: "Endpoint 未配置"
    val planner = settings.plannerModel?.trim()?.takeIf { it.isNotEmpty() } ?: "Planner 未配置"
    val replier =
            settings.replierModel?.trim()?.takeIf { it.isNotEmpty() } ?: "Replier 跟随 Planner"
    val tools = if (settings.nativeToolCalling) "Tools native" else "Tools compat"
    return "LLM: $endpoint · Planner: $planner · Replier: $replier · $tools"
}

private fun buildLocalLlmSettings(
        enabled: Boolean,
        baseUrl: String,
        apiKey: String,
        plannerModel: String,
        replierModel: String,
        nativeToolCalling: Boolean,
        environmentRepliesEnabled: Boolean,
        temperature: String,
        maxTokens: String,
        timeoutMillis: String
): LocalLlmSettings =
        LocalLlmSettings(
                enabled = enabled,
                baseUrl = baseUrl.trim().ifBlank { null },
                apiKey = apiKey.trim().ifBlank { null },
                plannerModel = plannerModel.trim().ifBlank { null },
                replierModel = replierModel.trim().ifBlank { null },
                nativeToolCalling = nativeToolCalling,
                environmentRepliesEnabled = environmentRepliesEnabled,
                temperature = temperature.trim().takeIf { it.isNotEmpty() }?.toDoubleOrNull(),
                maxTokens = maxTokens.trim().takeIf { it.isNotEmpty() }?.toIntOrNull(),
                timeoutMillis =
                        timeoutMillis.trim().takeIf { it.isNotEmpty() }?.toLongOrNull()
                                ?: LocalLlmSettings.DEFAULT_TIMEOUT_MILLIS
        )

private fun validateLocalLlmNumericInput(
        enabled: Boolean,
        temperature: String,
        maxTokens: String,
        timeoutMillis: String
): List<String> =
        RuntimeSettingsValidation.validateLocalLlmNumericInput(
                enabled,
                temperature,
                maxTokens,
                timeoutMillis
        )

@Composable
private fun AgentProfileConfigDialog(
        modelName: String?,
        importEvent: AgentProfileImportEvent?,
        onImportEventConsumed: (AgentProfileImportEvent) -> Unit,
        onImportRequest: () -> Unit,
        onExportJson: (String, String) -> Unit,
        onSaved: () -> Unit,
        onDismiss: () -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val database = remember(context) { ChatDatabase.getInstance(context.applicationContext) }
    val repository = remember(database) { AgentProfileRepository(database.runtimeStateDao()) }
    val agentId = remember(modelName) { agentIdFromModelName(modelName) }

    var isLoading by remember { mutableStateOf(false) }
    var isSaving by remember { mutableStateOf(false) }
    var loadError by remember { mutableStateOf<String?>(null) }
    var displayName by remember { mutableStateOf(modelName.orEmpty()) }
    var persona by remember { mutableStateOf("") }
    var provider by remember { mutableStateOf("") }
    var providerModel by remember { mutableStateOf("") }
    var settingsJson by remember { mutableStateOf("") }
    var plannerSystemPrompt by remember { mutableStateOf("") }
    var decisionSystemPrompt by remember { mutableStateOf("") }
    var replierSystemPrompt by remember { mutableStateOf("") }
    var replierUserPrompt by remember { mutableStateOf("") }

    fun applyProfile(profile: EditableAgentProfile) {
        displayName = profile.displayName
        persona = profile.persona.orEmpty()
        provider = profile.provider.orEmpty()
        providerModel = profile.model.orEmpty()
        settingsJson = profile.settingsJson.orEmpty()
        plannerSystemPrompt = profile.prompts[AgentPromptTemplateNames.PLANNER_SYSTEM].orEmpty()
        decisionSystemPrompt = profile.prompts[AgentPromptTemplateNames.DECISION_SYSTEM].orEmpty()
        replierSystemPrompt = profile.prompts[AgentPromptTemplateNames.REPLIER_SYSTEM].orEmpty()
        replierUserPrompt = profile.prompts[AgentPromptTemplateNames.REPLIER_USER].orEmpty()
    }

    fun currentProfile(): EditableAgentProfile? {
        val id = agentId ?: return null
        return EditableAgentProfile(
                agentId = id,
                displayName = displayName,
                persona = persona,
                provider = provider,
                model = providerModel,
                settingsJson = settingsJson,
                prompts =
                        mapOf(
                                AgentPromptTemplateNames.PLANNER_SYSTEM to plannerSystemPrompt,
                                AgentPromptTemplateNames.DECISION_SYSTEM to decisionSystemPrompt,
                                AgentPromptTemplateNames.REPLIER_SYSTEM to replierSystemPrompt,
                                AgentPromptTemplateNames.REPLIER_USER to replierUserPrompt
                        )
        )
    }

    LaunchedEffect(database, agentId, modelName) {
        val id = agentId
        if (id == null) {
            loadError = "当前模型没有可用的 Agent ID"
            return@LaunchedEffect
        }
        isLoading = true
        loadError = null
        val result =
                runCatching {
                    withContext(Dispatchers.IO) {
                        val stateDao = database.runtimeStateDao()
                        DefaultAgentProfileSeeder.seed(
                                context = context.applicationContext,
                                stateDao = stateDao,
                                agentId = id,
                                displayName = modelName
                        )
                        repository.load(id, modelName)
                    }
                }
        result.onSuccess { applyProfile(it) }
                .onFailure { error ->
                    uiLogger.error("加载角色配置失败", error)
                    loadError = error.message ?: "角色配置加载失败"
                }
        isLoading = false
    }

    LaunchedEffect(importEvent?.id, agentId) {
        val event = importEvent ?: return@LaunchedEffect
        val id = agentId
        if (id == null) {
            Toast.makeText(context, "当前模型没有可用的 Agent ID", Toast.LENGTH_SHORT).show()
            onImportEventConsumed(event)
            return@LaunchedEffect
        }
        val result =
                runCatching {
                    withContext(Dispatchers.IO) {
                        repository.importJson(
                                json = event.json,
                                targetAgentId = id,
                                fallbackDisplayName = modelName
                        )
                    }
                }
        result.onSuccess { profile ->
                    applyProfile(profile)
                    Toast.makeText(context, "角色配置已载入", Toast.LENGTH_SHORT).show()
                }
                .onFailure { error ->
                    uiLogger.error("解析角色配置失败", error)
                    Toast.makeText(context, "角色配置 JSON 无效", Toast.LENGTH_SHORT).show()
                }
        onImportEventConsumed(event)
    }

    AlertDialog(
            modifier = Modifier.testTag("agent-profile-dialog"),
            onDismissRequest = onDismiss,
            title = { Text("角色配置") },
            text = {
                Column(
                        modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    if (isLoading) {
                        LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                    }
                    loadError?.let { error ->
                        Text(
                                text = error,
                                color = MaterialTheme.colorScheme.error,
                                style = MaterialTheme.typography.bodySmall
                        )
                    }
                    OutlinedTextField(
                            value = agentId.orEmpty(),
                            onValueChange = {},
                            label = { Text("Agent ID") },
                            singleLine = true,
                            enabled = false,
                            modifier = Modifier.fillMaxWidth().testTag("agent-profile-agent-id")
                    )
                    OutlinedTextField(
                            value = displayName,
                            onValueChange = { displayName = it },
                            label = { Text("显示名称") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth().testTag("agent-profile-display-name")
                    )
                    OutlinedTextField(
                            value = persona,
                            onValueChange = { persona = it },
                            label = { Text("人格设定") },
                            minLines = 3,
                            maxLines = 6,
                            modifier = Modifier.fillMaxWidth().testTag("agent-profile-persona")
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(
                                value = provider,
                                onValueChange = { provider = it },
                                label = { Text("Provider") },
                                singleLine = true,
                                modifier = Modifier.weight(1f).testTag("agent-profile-provider")
                        )
                        OutlinedTextField(
                                value = providerModel,
                                onValueChange = { providerModel = it },
                                label = { Text("Model") },
                                singleLine = true,
                                modifier = Modifier.weight(1f).testTag("agent-profile-model")
                        )
                    }
                    OutlinedTextField(
                            value = settingsJson,
                            onValueChange = { settingsJson = it },
                            label = { Text("Settings JSON") },
                            minLines = 2,
                            maxLines = 5,
                            modifier = Modifier.fillMaxWidth().testTag("agent-profile-settings-json")
                    )
                    HorizontalDivider()
                    AgentPromptField(
                            label = "Planner system",
                            value = plannerSystemPrompt,
                            onValueChange = { plannerSystemPrompt = it },
                            tag = "agent-profile-planner-system"
                    )
                    AgentPromptField(
                            label = "Decision system",
                            value = decisionSystemPrompt,
                            onValueChange = { decisionSystemPrompt = it },
                            tag = "agent-profile-decision-system"
                    )
                    AgentPromptField(
                            label = "Replier system",
                            value = replierSystemPrompt,
                            onValueChange = { replierSystemPrompt = it },
                            tag = "agent-profile-replier-system"
                    )
                    AgentPromptField(
                            label = "Replier user",
                            value = replierUserPrompt,
                            onValueChange = { replierUserPrompt = it },
                            tag = "agent-profile-replier-user"
                    )
                }
            },
            confirmButton = {
                TextButton(
                        modifier = Modifier.testTag("agent-profile-save"),
                        enabled = !isLoading && !isSaving && agentId != null,
                        onClick = {
                            val profile = currentProfile()
                            if (profile == null) {
                                Toast.makeText(context, "当前模型没有可用的 Agent ID", Toast.LENGTH_SHORT)
                                        .show()
                                return@TextButton
                            }
                            scope.launch {
                                isSaving = true
                                val result =
                                        runCatching {
                                            withContext(Dispatchers.IO) {
                                                repository.save(profile)
                                            }
                                        }
                                withContext(Dispatchers.Main.immediate) {
                                    result.onSuccess {
                                                onSaved()
                                                Toast.makeText(
                                                                context,
                                                                "角色配置已保存",
                                                                Toast.LENGTH_SHORT
                                                        )
                                                        .show()
                                            }
                                            .onFailure { error ->
                                                uiLogger.error("保存角色配置失败", error)
                                                Toast.makeText(
                                                                context,
                                                                "角色配置保存失败",
                                                                Toast.LENGTH_SHORT
                                                        )
                                                        .show()
                                            }
                                    isSaving = false
                                }
                            }
                        }
                ) { Text(if (isSaving) "保存中" else "保存") }
            },
            dismissButton = {
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    TextButton(enabled = !isLoading && !isSaving, onClick = onImportRequest) {
                        Text("导入")
                    }
                    TextButton(
                            enabled = !isLoading && !isSaving && agentId != null,
                            onClick = {
                                val profile = currentProfile() ?: return@TextButton
                                val json = repository.exportJson(profile)
                                onExportJson(agentProfileExportFileName(profile.agentId), json)
                            }
                    ) {
                        Text("导出")
                    }
                    TextButton(enabled = !isSaving, onClick = onDismiss) { Text("关闭") }
                }
            }
    )
}

@Composable
private fun AgentPromptField(
        label: String,
        value: String,
        onValueChange: (String) -> Unit,
        tag: String
) {
    OutlinedTextField(
            value = value,
            onValueChange = onValueChange,
            label = { Text(label) },
            minLines = 4,
            maxLines = 8,
            modifier = Modifier.fillMaxWidth().testTag(tag)
    )
}

private tailrec fun Context.findActivity(): Activity? =
        when (this) {
            is Activity -> this
            is android.content.ContextWrapper -> baseContext.findActivity()
            else -> null
        }

@Composable
private fun WorkerConfigDialog(
        settings: WorkerLlmSettings,
        onSave: (WorkerLlmSettings) -> Unit,
        onDismiss: () -> Unit
) {
    var baseUrl by remember { mutableStateOf(settings.baseUrl.orEmpty()) }
    var apiKey by remember { mutableStateOf(settings.apiKey.orEmpty()) }
    var model by remember { mutableStateOf(settings.model.orEmpty()) }
    // The editable CC-format settings.json — starts from the stored JSON, else the model-only baseline.
    var json by remember {
        mutableStateOf(
                settings.settingsJson?.takeIf { it.isNotBlank() }
                        ?: WorkerLlmSettings.buildModelOnlyJson(
                                settings.baseUrl,
                                settings.apiKey,
                                settings.model
                        )
        )
    }
    // While true, the JSON is the auto baseline and is regenerated whenever a basic field changes.
    var jsonIsAuto by remember { mutableStateOf(settings.settingsJson.isNullOrBlank()) }
    var revertedNote by remember { mutableStateOf(false) }

    fun baseline(): String =
            WorkerLlmSettings.buildModelOnlyJson(baseUrl.trim(), apiKey.trim(), model.trim())
    fun onBasicChanged() {
        revertedNote = false
        if (jsonIsAuto) json = baseline()
    }

    val jsonValid = remember(json) { WorkerLlmSettings.isValidJsonObject(json) }

    AlertDialog(
            onDismissRequest = onDismiss,
            title = { Text("Worker / cc_research 配置") },
            text = {
                Column(
                        modifier =
                                Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Text(
                            "本机 worker（cc_research）使用的模型连接。填好下面三项即可，下方是发送给 worker 的 CC 格式 settings.json，可进一步编辑。",
                            style = MaterialTheme.typography.bodySmall
                    )
                    OutlinedTextField(
                            value = baseUrl,
                            onValueChange = {
                                baseUrl = it
                                onBasicChanged()
                            },
                            label = { Text("Base URL") },
                            placeholder = { Text("https://api.openai.com/v1") },
                            singleLine = true,
                            keyboardOptions =
                                    KeyboardOptions(keyboardType = KeyboardType.Uri),
                            modifier = Modifier.fillMaxWidth()
                    )
                    OutlinedTextField(
                            value = apiKey,
                            onValueChange = {
                                apiKey = it
                                onBasicChanged()
                            },
                            label = { Text("API Key") },
                            visualTransformation = PasswordVisualTransformation(),
                            keyboardOptions =
                                    KeyboardOptions(keyboardType = KeyboardType.Password),
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                    )
                    OutlinedTextField(
                            value = model,
                            onValueChange = {
                                model = it
                                onBasicChanged()
                            },
                            label = { Text("模型 ID") },
                            placeholder = { Text("qwen-plus") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                    )
                    Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween,
                            modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                                "settings.json (CC 格式，可编辑)",
                                style = MaterialTheme.typography.labelLarge
                        )
                        TextButton(
                                onClick = {
                                    json = baseline()
                                    jsonIsAuto = true
                                    revertedNote = false
                                }
                        ) { Text("重置为模型配置") }
                    }
                    OutlinedTextField(
                            value = json,
                            onValueChange = {
                                json = it
                                jsonIsAuto = false
                                revertedNote = false
                            },
                            isError = !jsonValid,
                            textStyle =
                                    LocalTextStyle.current.copy(
                                            fontFamily = FontFamily.Monospace,
                                            fontSize = 12.sp
                                    ),
                            modifier =
                                    Modifier.fillMaxWidth().heightIn(min = 150.dp, max = 320.dp)
                    )
                    when {
                        !jsonValid ->
                                Text(
                                        "JSON 无效，保存时将恢复为仅模型配置。",
                                        color = MaterialTheme.colorScheme.error,
                                        style = MaterialTheme.typography.bodySmall
                                )
                        revertedNote ->
                                Text(
                                        "JSON 有误，已恢复为仅模型配置，请确认后再次保存。",
                                        color = MaterialTheme.colorScheme.primary,
                                        style = MaterialTheme.typography.bodySmall
                                )
                    }
                }
            },
            confirmButton = {
                TextButton(
                        onClick = {
                            if (jsonValid && json.isNotBlank()) {
                                val isBaseline = json.trim() == baseline().trim()
                                onSave(
                                        WorkerLlmSettings(
                                                baseUrl = baseUrl.trim().ifBlank { null },
                                                apiKey = apiKey.trim().ifBlank { null },
                                                model = model.trim().ifBlank { null },
                                                settingsJson = if (isBaseline) null else json,
                                        )
                                )
                            } else {
                                // 报错 → 恢复为仅模型配置的 JSON（保持打开，待用户确认后再保存）
                                json = baseline()
                                jsonIsAuto = true
                                revertedNote = true
                            }
                        }
                ) { Text("保存") }
            },
            dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } }
    )
}

@Composable
private fun ConnectionConfigDialog(
        nickname: String,
        localLlmSettings: LocalLlmSettings,
        onNicknameChange: (String) -> Unit,
        onSave: (Boolean, LocalLlmSettings) -> Unit,
        onDismiss: () -> Unit
) {
    var tempNickname by remember { mutableStateOf(nickname) }
    var tempLocalEnabled by remember { mutableStateOf(localLlmSettings.enabled) }
    var tempLocalBaseUrl by remember { mutableStateOf(localLlmSettings.baseUrl.orEmpty()) }
    var tempLocalApiKey by remember { mutableStateOf(localLlmSettings.apiKey.orEmpty()) }
    var tempPlannerModel by remember { mutableStateOf(localLlmSettings.plannerModel.orEmpty()) }
    var tempReplierModel by remember { mutableStateOf(localLlmSettings.replierModel.orEmpty()) }
    var tempNativeTools by remember { mutableStateOf(localLlmSettings.nativeToolCalling) }
    var tempEnvReplies by remember { mutableStateOf(localLlmSettings.environmentRepliesEnabled) }
    var tempTemperature by remember {
        mutableStateOf(localLlmSettings.temperature?.toString().orEmpty())
    }
    var tempMaxTokens by remember {
        mutableStateOf(localLlmSettings.maxTokens?.toString().orEmpty())
    }
    var tempTimeoutMillis by remember {
        mutableStateOf(
                localLlmSettings.timeoutMillis?.toString()
                        ?: LocalLlmSettings.DEFAULT_TIMEOUT_MILLIS.toString()
        )
    }
    var showErrors by remember { mutableStateOf(false) }
    val nextLocalLlmSettings =
            remember(
                    tempLocalEnabled,
                    tempLocalBaseUrl,
                    tempLocalApiKey,
                    tempPlannerModel,
                    tempReplierModel,
                    tempNativeTools,
                    tempEnvReplies,
                    tempTemperature,
                    tempMaxTokens,
                    tempTimeoutMillis
            ) {
                buildLocalLlmSettings(
                        enabled = tempLocalEnabled,
                        baseUrl = tempLocalBaseUrl,
                        apiKey = tempLocalApiKey,
                        plannerModel = tempPlannerModel,
                        replierModel = tempReplierModel,
                        nativeToolCalling = tempNativeTools,
                        environmentRepliesEnabled = tempEnvReplies,
                        temperature = tempTemperature,
                        maxTokens = tempMaxTokens,
                        timeoutMillis = tempTimeoutMillis
                )
            }
    val errors =
            remember(
                    nextLocalLlmSettings,
                    tempTemperature,
                    tempMaxTokens,
                    tempTimeoutMillis
            ) {
                validateConfig(nextLocalLlmSettings) +
                        validateLocalLlmNumericInput(
                                enabled = tempLocalEnabled,
                                temperature = tempTemperature,
                                maxTokens = tempMaxTokens,
                                timeoutMillis = tempTimeoutMillis
                        )
            }
    AlertDialog(
            onDismissRequest = onDismiss,
            title = { Text("运行设置") },
            text = {
                Column(
                        modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    OutlinedTextField(
                            value = tempNickname,
                            onValueChange = {
                                tempNickname = it
                                onNicknameChange(it)
                            },
                            label = { Text("我的昵称 (可选)") },
                            placeholder = { Text("留空使用默认: 我") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                    )
                    HorizontalDivider()
                    Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("启用 LLM Provider")
                        Switch(
                                checked = tempLocalEnabled,
                                onCheckedChange = { tempLocalEnabled = it }
                        )
                    }
                    if (tempLocalEnabled) {
                        OutlinedTextField(
                                value = tempLocalBaseUrl,
                                onValueChange = { tempLocalBaseUrl = it },
                                label = { Text("LLM Endpoint") },
                                placeholder = { Text("http://127.0.0.1:11434/v1") },
                                singleLine = true,
                                isError =
                                        showErrors &&
                                                errors.any { it.contains("LLM Endpoint") },
                                keyboardOptions =
                                        KeyboardOptions(keyboardType = KeyboardType.Uri),
                                modifier = Modifier.fillMaxWidth()
                        )
                        OutlinedTextField(
                                value = tempLocalApiKey,
                                onValueChange = { tempLocalApiKey = it },
                                label = { Text("API Key(可选)") },
                                singleLine = true,
                                visualTransformation = PasswordVisualTransformation(),
                                keyboardOptions =
                                        KeyboardOptions(keyboardType = KeyboardType.Password),
                                modifier = Modifier.fillMaxWidth()
                        )
                        OutlinedTextField(
                                value = tempPlannerModel,
                                onValueChange = { tempPlannerModel = it },
                                label = { Text("Planner 模型") },
                                singleLine = true,
                                isError =
                                        showErrors &&
                                                errors.any { it.contains("Planner 模型") },
                                modifier = Modifier.fillMaxWidth()
                        )
                        OutlinedTextField(
                                value = tempReplierModel,
                                onValueChange = { tempReplierModel = it },
                                label = { Text("Replier 模型(可选)") },
                                singleLine = true,
                                modifier = Modifier.fillMaxWidth()
                        )
                        Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("原生工具调用")
                            Switch(
                                    checked = tempNativeTools,
                                    onCheckedChange = { tempNativeTools = it }
                            )
                        }
                        Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("环境事件回复(计费)")
                            Switch(
                                    checked = tempEnvReplies,
                                    onCheckedChange = { tempEnvReplies = it }
                            )
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedTextField(
                                    value = tempTemperature,
                                    onValueChange = { tempTemperature = it },
                                    label = { Text("Temperature") },
                                    singleLine = true,
                                    isError =
                                            showErrors &&
                                                    errors.any { it.contains("Temperature") },
                                    keyboardOptions =
                                            KeyboardOptions(keyboardType = KeyboardType.Decimal),
                                    modifier = Modifier.weight(1f)
                            )
                            OutlinedTextField(
                                    value = tempMaxTokens,
                                    onValueChange = { tempMaxTokens = it },
                                    label = { Text("Max tokens") },
                                    singleLine = true,
                                    isError =
                                            showErrors &&
                                                    errors.any { it.contains("Max tokens") },
                                    keyboardOptions =
                                            KeyboardOptions(keyboardType = KeyboardType.Number),
                                    modifier = Modifier.weight(1f)
                            )
                        }
                        OutlinedTextField(
                                value = tempTimeoutMillis,
                                onValueChange = { tempTimeoutMillis = it },
                                label = { Text("超时(ms)") },
                                singleLine = true,
                                isError = showErrors && errors.any { it.contains("超时") },
                                keyboardOptions =
                                        KeyboardOptions(keyboardType = KeyboardType.Number),
                                modifier = Modifier.fillMaxWidth()
                        )
                    }
                    if (showErrors && errors.isNotEmpty()) {
                        errors.forEach { err ->
                            Text(
                                    err,
                                    color = MaterialTheme.colorScheme.error,
                                    style = MaterialTheme.typography.bodySmall
                            )
                        }
                    } else {
                        val summary = buildString {
                            append("将使用此运行设置：\n")
                            append("运行模式: 本地运行时\n")
                            if (nextLocalLlmSettings.enabled) {
                                append(
                                        "LLM: ${nextLocalLlmSettings.baseUrl ?: "(未填写)"} / ${nextLocalLlmSettings.plannerModel ?: "(未填写)"}\n"
                                )
                            } else {
                                append("LLM Provider: 未启用\n")
                            }
                            append("我: ${tempNickname.ifBlank { "我" }}")
                        }
                        Text(
                                summary,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.secondary
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(
                        onClick = {
                            if (errors.isEmpty()) {
                                onSave(true, nextLocalLlmSettings)
                            } else {
                                showErrors = true
                            }
                        }
                ) { Text("保存") }
            },
            dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } }
    )
}

private fun agentIdFromModelName(modelName: String?): String? =
        modelName?.trim()?.takeIf { it.isNotEmpty() }
                ?.lowercase()
                ?.replace(Regex("[^a-z0-9_-]+"), "_")
                ?.takeIf { it.isNotEmpty() }

private fun agentProfileExportFileName(agentId: String): String =
        "maimchat_${agentId.ifBlank { "agent" }}_profile.json"

private suspend fun readTextFromUri(context: Context, uri: Uri): String? =
        withContext(Dispatchers.IO) {
            try {
                context.contentResolver.openInputStream(uri)?.bufferedReader()?.use {
                    it.readText()
                }
            } catch (e: Exception) {
                uiLogger.error("读取角色配置失败", e)
                null
            }
        }

private suspend fun writeTextToUri(context: Context, uri: Uri, text: String): Boolean =
        withContext(Dispatchers.IO) {
            try {
                val output = context.contentResolver.openOutputStream(uri) ?: return@withContext false
                output.bufferedWriter().use { it.write(text) }
                true
            } catch (e: Exception) {
                uiLogger.error("写入角色配置失败", e)
                false
            }
        }

private suspend fun loadBackgroundBitmap(path: String): Bitmap? =
        withContext(Dispatchers.IO) {
            try {
                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeFile(path, bounds)
                if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return@withContext null
                val sample = calculateInSampleSize(bounds.outWidth, bounds.outHeight, 2048)
                val decodeOptions =
                        BitmapFactory.Options().apply {
                            inSampleSize = sample
                            inPreferredConfig = Bitmap.Config.ARGB_8888
                        }
                BitmapFactory.decodeFile(path, decodeOptions)
            } catch (e: Exception) {
                uiLogger.error("加载背景位图失败", e)
                null
            }
        }

private suspend fun copyImageToInternal(context: Context, uri: Uri): String? =
        withContext(Dispatchers.IO) {
            val resolver = context.contentResolver
            try {
                val dir = File(context.filesDir, "wallpaper")
                if (!dir.exists()) dir.mkdirs()

                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                openInputStream(context, uri)?.use { input ->
                    BitmapFactory.decodeStream(input, null, bounds)
                }
                if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return@withContext null

                val sampleSize = calculateInSampleSize(bounds.outWidth, bounds.outHeight, 2048)
                val decodeOptions =
                        BitmapFactory.Options().apply {
                            inSampleSize = sampleSize
                            inPreferredConfig = Bitmap.Config.ARGB_8888
                        }

                val decoded =
                        openInputStream(context, uri)?.use { input ->
                            BitmapFactory.decodeStream(input, null, decodeOptions)
                        }
                                ?: return@withContext null

                val processedBitmap =
                        if (max(decoded.width, decoded.height) > 2048) {
                            val scale = 2048f / max(decoded.width, decoded.height)
                            Bitmap.createScaledBitmap(
                                            decoded,
                                            (decoded.width * scale).roundToInt().coerceAtLeast(1),
                                            (decoded.height * scale).roundToInt().coerceAtLeast(1),
                                            true
                                    )
                                    .also { decoded.recycle() }
                        } else {
                            decoded
                        }

                val mime = resolver.getType(uri)?.lowercase().orEmpty()
                val isPng = mime.contains("png")
                val extension = if (isPng) "png" else "jpg"
                val format = if (isPng) Bitmap.CompressFormat.PNG else Bitmap.CompressFormat.JPEG
                val targetFile = File(dir, "wallpaper_${System.currentTimeMillis()}.$extension")

                FileOutputStream(targetFile).use { output ->
                    processedBitmap.compress(format, 90, output)
                    output.flush()
                }

                processedBitmap.recycle()

                dir.listFiles()?.forEach { file ->
                    if (file.absolutePath != targetFile.absolutePath &&
                                    file.name.startsWith("wallpaper_")
                    ) {
                        file.delete()
                    }
                }

                targetFile.absolutePath
            } catch (e: Exception) {
                uiLogger.error("复制壁纸图片失败", e)
                null
            }
        }

private fun deleteTempUri(context: Context, uri: Uri) {
    when (uri.scheme?.lowercase()) {
        "file" -> {
            val path = uri.path
            if (!path.isNullOrBlank()) {
                try {
                    File(path).takeIf { it.exists() }?.delete()
                } catch (_: Exception) {}
            }
        }
        "content" -> {
            try {
                context.contentResolver.delete(uri, null, null)
            } catch (_: Exception) {}
        }
    }
}

private fun openInputStream(context: Context, uri: Uri): InputStream? {
    return try {
        when (uri.scheme?.lowercase()) {
            "content" -> context.contentResolver.openInputStream(uri)
            "file" ->
                    uri.path?.let { path ->
                        File(path).takeIf { it.exists() }?.let { FileInputStream(it) }
                    }
            else -> context.contentResolver.openInputStream(uri)
        }
    } catch (e: Exception) {
        uiLogger.error("打开图片流失败", e)
        null
    }
}

private fun Context.deleteTempCacheFile(file: File) {
    try {
        if (file.exists()) file.delete()
    } catch (_: Exception) {}
}

private fun grantUriToCropApp(context: Context, uri: Uri) {
    val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
    try {
        context.grantUriPermission(context.packageName, uri, flags)
        context.grantUriPermission("com.yalantis.ucrop", uri, flags)
    } catch (e: Exception) {
        uiLogger.warn("授权裁剪应用访问 URI 失败", e)
    }
}

private fun calculateInSampleSize(width: Int, height: Int, maxDim: Int): Int {
    if (width <= 0 || height <= 0 || maxDim <= 0) return 1
    var sampleSize = 1
    while (max(width / sampleSize, height / sampleSize) > maxDim) {
        sampleSize *= 2
    }
    return sampleSize
}

private fun buildEnvironmentVisualSnapshotReference(
        source: String,
        modelFolderPath: String?,
        modelName: String?,
        lifecycleState: String?,
        backgroundPath: String?,
        width: Int,
        height: Int
): String {
    val fingerprint =
            listOf(
                            source,
                            modelFolderPath.orEmpty(),
                            modelName.orEmpty(),
                            lifecycleState.orEmpty(),
                            backgroundPath.orEmpty(),
                            width.coerceAtLeast(0).toString(),
                            height.coerceAtLeast(0).toString()
                    )
                    .joinToString("|")
                    .hashCode()
    return "$source:${Integer.toHexString(fingerprint)}"
}

private fun sendWallpaperRefreshBroadcast(context: Context, path: String?) {
    val intent =
            Intent(WallpaperComm.ACTION_REFRESH_BACKGROUND).apply {
                putExtra(WallpaperComm.EXTRA_BACKGROUND_PATH, path)
                // The wallpaper engine runs in the :wallpaper process with a
                // RECEIVER_NOT_EXPORTED receiver; scope the broadcast to this app so it is
                // actually delivered (implicit broadcasts skip not-exported receivers).
                setPackage(context.packageName)
            }
    context.sendBroadcast(intent)
}

private fun sendWallpaperModelBroadcast(context: Context, folder: String?) {
    val intent =
            Intent(WallpaperComm.ACTION_REFRESH_MODEL).apply {
                putExtra(WallpaperComm.EXTRA_MODEL_FOLDER, folder)
                setPackage(context.packageName)
            }
    context.sendBroadcast(intent)
}

private fun sendWallpaperBubbleCountBroadcast(context: Context, count: Int) {
    val intent =
            Intent(WallpaperComm.ACTION_REFRESH_BUBBLE_COUNT).apply {
                putExtra(WallpaperComm.EXTRA_BUBBLE_COUNT, count)
                setPackage(context.packageName)
            }
    context.sendBroadcast(intent)
}
