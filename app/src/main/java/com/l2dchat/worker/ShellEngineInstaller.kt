package com.l2dchat.worker

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.util.Log
import androidx.core.content.ContextCompat
import java.io.File

/**
 * Installs the bundled headless engine package (com.l2dchat.shell) via PackageInstaller. The engine
 * APK ships inside this app's assets (assets/engine/engine.apk). Install is NOT silent — the system
 * shows a confirm dialog (needs REQUEST_INSTALL_PACKAGES + the user's "install unknown apps" grant);
 * truly silent install would require root or device-owner.
 */
class ShellEngineInstaller(private val context: Context) {

    private val TAG = "ShellEngineInstaller"
    private val action = "com.l2dchat.worker.ENGINE_INSTALL_RESULT"

    fun isEngineInstalled(): Boolean =
            runCatching { context.packageManager.getPackageInfo(ShellEngineProtocol.ENGINE_PACKAGE, 0) }
                    .isSuccess

    /** Stream the bundled engine APK into a PackageInstaller session and commit it. */
    fun install(activity: Activity, onDone: (success: Boolean, message: String?) -> Unit) {
        try {
            val apk = File(context.cacheDir, "engine.apk")
            context.assets.open("engine/engine.apk").use { i -> apk.outputStream().use { i.copyTo(it) } }

            val installer = context.packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
            val sessionId = installer.createSession(params)
            installer.openSession(sessionId).use { session ->
                apk.inputStream().use { input ->
                    session.openWrite("engine", 0, apk.length()).use { out ->
                        input.copyTo(out); session.fsync(out)
                    }
                }

                val receiver = object : BroadcastReceiver() {
                    override fun onReceive(c: Context, intent: Intent) {
                        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999)) {
                            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                                @Suppress("DEPRECATION")
                                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                                confirm?.let { activity.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                            }
                            PackageInstaller.STATUS_SUCCESS -> {
                                runCatching { c.unregisterReceiver(this) }
                                onDone(true, null)
                            }
                            else -> {
                                runCatching { c.unregisterReceiver(this) }
                                val m = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
                                Log.w(TAG, "install failed: $m")
                                onDone(false, m)
                            }
                        }
                    }
                }
                ContextCompat.registerReceiver(
                        context, receiver, IntentFilter(action), ContextCompat.RECEIVER_NOT_EXPORTED
                )
                val pending = PendingIntent.getBroadcast(
                        context, sessionId, Intent(action).setPackage(context.packageName),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
                )
                session.commit(pending.intentSender)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "install error", t)
            onDone(false, t.message)
        }
    }
}
