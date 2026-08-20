plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
}

import java.util.Properties

// 读取签名属性需在 android 块外创建（Kotlin DSL 推荐）
val signingPropsFile = rootProject.file("signing.properties")
val signingProps = Properties().apply {
    if (signingPropsFile.exists()) {
        load(signingPropsFile.inputStream())
    }
}

// 默认 LLM provider 凭据仅从本地（gitignored）local.properties 注入，绝不入库。
// 一个干净检出里这些值为空，应用即出厂未配置；本地 debug 构建可在 local.properties 写入：
//   llm.default.baseUrl=...   llm.default.apiKey=...   llm.default.plannerModel=...
val localPropsFile = rootProject.file("local.properties")
val localProps = Properties().apply {
    if (localPropsFile.exists()) {
        load(localPropsFile.inputStream())
    }
}
fun llmDefault(key: String): String = localProps.getProperty(key, "")

// mem embedding provider base url is also a secret-ish default injected only in debug from
// local.properties (key: mem.default.embeddingBaseUrl). Same mechanism as llmDefault.
fun memDefault(key: String): String = localProps.getProperty(key, "")

android {
    namespace = "com.l2dchat"
    compileSdk = 36

    defaultConfig {
    applicationId = "com.l2dchat"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // Provider (base_url + api_key) is a SECRET: left blank here and injected ONLY in the debug
        // build type from the gitignored local.properties. Model ids + planner-thinking are NOT
        // secret and ARE baked here (committed to git), so every build (debug & release) defaults to
        // this role split: planner=step (thinking on), replier=qwen, worker=kimi.
        buildConfigField("String", "LLM_DEFAULT_BASE_URL", "\"\"")
        buildConfigField("String", "LLM_DEFAULT_API_KEY", "\"\"")
        buildConfigField("String", "LLM_DEFAULT_PLANNER_MODEL", "\"stepfun/step-3.7-flash\"")
        buildConfigField("String", "LLM_DEFAULT_REPLIER_MODEL", "\"qwen3.7-plus\"")
        buildConfigField("String", "LLM_DEFAULT_WORKER_MODEL", "\"kimi-k2.7-code\"")
        buildConfigField("boolean", "LLM_DEFAULT_PLANNER_THINKING", "true")

        // mem subsystem defaults. Model ids + dimensions are NOT secret and ARE committed so every
        // build ships with a sensible role split. The embedding base_url is blank here and injected
        // only in the debug build type from local.properties (mem.default.embeddingBaseUrl).
        buildConfigField("String", "MEM_DEFAULT_EXTRACTION_MODEL", "\"qwen3.7-max\"")
        buildConfigField("String", "MEM_DEFAULT_EXTRACTION_MM_MODEL", "\"qwen3.7-plus\"")
        buildConfigField("String", "MEM_DEFAULT_EMBEDDING_MODEL", "\"Qwen/Qwen3-Embedding-0.6B\"")
        buildConfigField("String", "MEM_DEFAULT_EMBEDDING_BASE_URL", "\"\"")
        buildConfigField("int", "MEM_DEFAULT_EMBEDDING_DIMENSIONS", "1024")
    }

    signingConfigs {
        create("release") {
            if (!signingPropsFile.exists()) {
                return@create
            }
            val storePath = signingProps.getProperty("storeFile")
                ?: throw GradleException("signing.properties 缺少 storeFile")
            storeFile = rootProject.file(storePath)
            storePassword = signingProps.getProperty("storePassword")
                ?: throw GradleException("signing.properties 缺少 storePassword")
            keyAlias = signingProps.getProperty("keyAlias")
                ?: throw GradleException("signing.properties 缺少 keyAlias")
            keyPassword = signingProps.getProperty("keyPassword")
                ?: throw GradleException("signing.properties 缺少 keyPassword")
        }
    }

    buildTypes {
        release {
            // 打开混淆与资源压缩（如不需要可改为 false）
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            // 保持 debug 可读性
            isMinifyEnabled = false
            // 仅本地 debug 构建从 local.properties 注入默认 provider 凭据（base_url + api_key，不入库）。
            // 模型 id 与 thinking 已在 defaultConfig 硬编码（入库），此处不再覆盖。
            buildConfigField("String", "LLM_DEFAULT_BASE_URL", "\"${llmDefault("llm.default.baseUrl")}\"")
            buildConfigField("String", "LLM_DEFAULT_API_KEY", "\"${llmDefault("llm.default.apiKey")}\"")
            // mem embedding provider default (secret-ish, debug-only, gitignored).
            buildConfigField("String", "MEM_DEFAULT_EMBEDDING_BASE_URL", "\"${memDefault("mem.default.embeddingBaseUrl")}\"")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        isCoreLibraryDesugaringEnabled = true
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    testOptions {
        unitTests {
            isIncludeAndroidResources = true
        }
    }
    sourceSets {
        getByName("androidTest").assets.srcDirs("$projectDir/schemas")
    }
}

ksp {
    arg("room.schemaLocation", "$projectDir/schemas")
}

tasks.register("validateReleaseSigning") {
    group = "verification"
    description = "Fail release builds early when signing.properties is missing or incomplete"

    doFirst {
        if (!signingPropsFile.exists()) {
            throw GradleException("缺少 signing.properties，请创建并填写签名信息后再构建 release")
        }

        val missingKeys = listOf("storeFile", "storePassword", "keyAlias", "keyPassword")
            .filter { signingProps.getProperty(it).isNullOrBlank() }
        if (missingKeys.isNotEmpty()) {
            throw GradleException("signing.properties 缺少 ${missingKeys.joinToString(", ")}")
        }
    }
}

tasks.configureEach {
    if (name.contains("Release")) {
        dependsOn("validateReleaseSigning")
    }
}

dependencies {

    coreLibraryDesugaring(libs.desugar.jdk.libs)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    implementation(libs.okhttp)
    implementation(libs.androidx.constraintlayout)
    implementation(libs.androidx.material.icons.extended)
    implementation(libs.gson)  // 添加Gson依赖
    implementation(libs.ucrop)
    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)
    testImplementation(libs.junit)
    testImplementation(libs.androidx.room.testing)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.room.testing)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.ui.test.junit4)
    debugImplementation(libs.androidx.ui.tooling)
    debugImplementation(libs.androidx.ui.test.manifest)
    implementation(files("libs/Live2DCubismCore.aar"))
    implementation(project(":framework"))
}

// --- APK 发布任务 ---
val publishedApkDir = rootProject.layout.buildDirectory.dir("published-apks")

tasks.register<Copy>("publishDebugApk") {
    group = "distribution"
    description = "Assemble the debug build and copy the APK to build/published-apks/debug"
    dependsOn("assembleDebug")
    from(layout.buildDirectory.dir("outputs/apk/debug"))
    include("*.apk")
    into(publishedApkDir.map { it.dir("debug") })
}

tasks.register<Copy>("publishReleaseApk") {
    group = "distribution"
    description = "Assemble the release build and copy the APK to build/published-apks/release"
    dependsOn("assembleRelease")
    from(layout.buildDirectory.dir("outputs/apk/release"))
    include("*.apk")
    into(publishedApkDir.map { it.dir("release") })
}

tasks.register("publishAllApks") {
    group = "distribution"
    description = "Build and copy both debug and release APKs into build/published-apks"
    dependsOn("publishDebugApk", "publishReleaseApk")
}

// --- Bundle the headless worker engine APK as an asset so the app can auto-install it ---
android { sourceSets.getByName("main").assets.srcDir(layout.buildDirectory.dir("generated/engineAssets")) }

// Expose the exported Room schemas to androidTest as assets so MigrationTestHelper can load
// the schema JSONs (room.schemaLocation == "$projectDir/schemas") and validate migrations.
android { sourceSets.getByName("androidTest").assets.srcDir("$projectDir/schemas") }

val copyEngineApk by tasks.registering(Copy::class) {
    dependsOn(":engine:assembleDebug")
    from(project(":engine").layout.buildDirectory.file("outputs/apk/debug/engine-debug.apk"))
    into(layout.buildDirectory.dir("generated/engineAssets/engine"))
    rename { "engine.apk" }
}
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }
        .configureEach { dependsOn(copyEngineApk) }
