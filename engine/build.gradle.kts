plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

import java.util.Properties

// PoC: inject the qwen/OpenAI-compatible creds from the (gitignored) root local.properties,
// same mechanism as :app. In the real engine these arrive at runtime over IPC, never baked in.
val localPropsFile = rootProject.file("local.properties")
val localProps = Properties().apply { if (localPropsFile.exists()) load(localPropsFile.inputStream()) }
fun llmDefault(key: String): String = localProps.getProperty(key, "")

android {
    namespace = "com.l2dchat.shell"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.l2dchat.shell"
        minSdk = 24
        // KEY: targetSdk 28 keeps the SELinux domain that still allows execve of binaries in the
        // app's writable filesDir (the W^X exemption). This is the whole point of the separate pkg.
        targetSdk = 28
        versionCode = 1
        versionName = "1.0"

        buildConfigField("String", "LLM_BASE_URL", "\"\"")
        buildConfigField("String", "LLM_API_KEY", "\"\"")
        buildConfigField("String", "LLM_MODEL", "\"\"")
    }

    buildTypes {
        debug {
            buildConfigField("String", "LLM_BASE_URL", "\"${llmDefault("llm.default.baseUrl")}\"")
            buildConfigField("String", "LLM_API_KEY", "\"${llmDefault("llm.default.apiKey")}\"")
            buildConfigField("String", "LLM_MODEL", "\"${llmDefault("llm.default.plannerModel")}\"")
        }
    }

    buildFeatures { buildConfig = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
}
