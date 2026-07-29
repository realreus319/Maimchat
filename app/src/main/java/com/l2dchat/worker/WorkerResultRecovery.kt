package com.l2dchat.worker

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Message
import android.os.Messenger
import android.util.Log

/**
 * Recovers worker results that completed while `:chat` was dead (OS reap while the app was
 * backgrounded mid-task). The engine keeps running such a task to completion and durably buffers its
 * result; this binds the engine on `:chat` startup, registers (which makes the engine flush every
 * buffered result), routes each back to its originating conversation via [deliver], and acks it so
 * the engine drops the buffer. Best-effort and short-lived: it unbinds after a brief collection
 * window. See ShellService's pending-result buffer + flushPendingResults.
 */
class WorkerResultRecovery(
    private val context: Context,
    /** Replay a recovered result into its origin conversation (contextId, agentId, messageId, text, isError). */
    private val deliver: (String, String, String, String, Boolean) -> Unit,
) {
    private val TAG = "WorkerResultRecovery"

    /** Bind + register + collect flushed results for [windowMs], then unbind. Never throws. */
    fun recover(windowMs: Long = 12_000) {
        val ht = HandlerThread("worker-recovery").apply { start() }
        var conn: ServiceConnection? = null
        val svcRef = java.util.concurrent.atomic.AtomicReference<Messenger?>(null)

        fun ack(taskId: String) {
            val svc = svcRef.get() ?: return
            runCatching {
                svc.send(Message.obtain(null, ShellEngineProtocol.MSG_RESULT_ACK).apply {
                    data = Bundle().apply { putString(ShellEngineProtocol.KEY_TASK_ID, taskId) }
                })
            }
        }

        fun handle(msg: Message) {
            val taskId = msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) ?: return
            val ctxId = msg.data.getString(ShellEngineProtocol.KEY_ORIGIN_CONTEXT)
            val agentId = msg.data.getString(ShellEngineProtocol.KEY_ORIGIN_AGENT)
            val messageId = msg.data.getString(ShellEngineProtocol.KEY_ORIGIN_MESSAGE)
            if (ctxId == null || agentId == null || messageId == null) {
                // No origin to route by (e.g. a debug task) — can't replay; still ack to drop it.
                Log.w(TAG, "buffered result $taskId has no origin; dropping")
                ack(taskId); return
            }
            val isError = msg.what == ShellEngineProtocol.MSG_ERROR
            val text = if (isError) msg.data.getString(ShellEngineProtocol.KEY_ERROR).orEmpty()
                       else msg.data.getString(ShellEngineProtocol.KEY_TEXT).orEmpty()
            Log.i(TAG, "recovering buffered result $taskId -> ctx=$ctxId msg=$messageId (error=$isError)")
            runCatching { deliver(ctxId, agentId, messageId, text, isError) }
                .onFailure { Log.w(TAG, "deliver failed: ${it.message}") }
            ack(taskId)
        }

        val incoming = Messenger(Handler(ht.looper) { msg ->
            when (msg.what) {
                ShellEngineProtocol.MSG_RESULT, ShellEngineProtocol.MSG_ERROR -> handle(msg)
            }
            true
        })

        fun cleanup() {
            runCatching { conn?.let { context.unbindService(it) } }
            ht.quitSafely()
        }

        conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                val svc = Messenger(binder)
                svcRef.set(svc)
                // MSG_REGISTER makes the engine flush every buffered result to `incoming`.
                runCatching {
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_REGISTER).apply { replyTo = incoming })
                }
            }
            override fun onServiceDisconnected(name: ComponentName?) {}
        }

        val intent = Intent()
            .setComponent(ComponentName(ShellEngineProtocol.ENGINE_PACKAGE, ShellEngineProtocol.SERVICE_CLASS))
            .setPackage(ShellEngineProtocol.ENGINE_PACKAGE)
        val bound = runCatching { context.bindService(intent, conn, Context.BIND_AUTO_CREATE) }.getOrDefault(false)
        if (!bound) { Log.w(TAG, "recovery bind failed"); cleanup(); return }
        // Collect the flush, then unbind. (Results arrive within milliseconds of register.)
        Handler(ht.looper).postDelayed({ cleanup() }, windowMs)
    }
}
