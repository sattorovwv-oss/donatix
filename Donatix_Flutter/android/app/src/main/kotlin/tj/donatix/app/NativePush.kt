package tj.donatix.app

import android.content.Context
import androidx.core.app.NotificationManagerCompat
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import androidx.work.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import java.util.concurrent.TimeUnit

object NativePush {
    private const val WORK = "donatix-fcm-register"
    fun configured(c: Context): Boolean = FirebaseApp.getApps(c).isNotEmpty()
    fun deviceId(c: Context): String {
        val p = NotificationSession.prefs(c)
        val id = p.getString("push_device", null) ?: UUID.randomUUID().toString().replace("-", "")
        p.edit().putString("push_device", id).apply()
        return id
    }
    fun register(c: Context, replace: Boolean = false) {
        if (!configured(c) || NotificationSession.prefs(c).getInt("userId", 0) == 0) return
        val prefs = NotificationSession.prefs(c)
        if (!replace && prefs.getBoolean("push_registered", false) && prefs.getBoolean("push_server", false)) return
        FirebaseMessaging.getInstance().isAutoInitEnabled = true
        val request = OneTimeWorkRequestBuilder<PushRegistrationWorker>()
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(c).enqueueUniqueWork(WORK,
            if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP, request)
    }
    fun stop(c: Context) {
        WorkManager.getInstance(c).cancelUniqueWork(WORK)
        if (configured(c)) {
            FirebaseMessaging.getInstance().isAutoInitEnabled = false
            FirebaseMessaging.getInstance().deleteToken().addOnCompleteListener {
                // Logout/login may finish while token deletion is still running.
                if (NotificationSession.prefs(c).getInt("userId", 0) != 0) register(c, replace = true)
            }
        }
    }
    fun status(c: Context): Map<String, Any> = mapOf(
        "firebase" to configured(c),
        "registered" to NotificationSession.prefs(c).getBoolean("push_registered", false),
        "server" to NotificationSession.prefs(c).getBoolean("push_server", false),
        "permission" to NotificationManagerCompat.from(c).areNotificationsEnabled(),
        "deviceId" to deviceId(c)
    )
}

class PushRegistrationWorker(c: Context, params: WorkerParameters) : Worker(c, params) {
    private fun exchange(origin: String, cookie: String, csrf: String?, body: JSONObject?): JSONObject {
        val endpoint = if (body == null) "/api/v1/mobile-session" else "/api/v1/mobile/push/register"
        val connection = URL(origin.trimEnd('/') + endpoint).openConnection() as HttpURLConnection
        try {
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 15000; connection.readTimeout = 20000
            connection.setRequestProperty("Cookie", "dx_session=$cookie")
            connection.setRequestProperty("Accept", "application/json")
            if (body != null) {
                connection.requestMethod = "POST"; connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.setRequestProperty("X-CSRF-Token", csrf!!)
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            if (connection.responseCode !in 200..299) throw IllegalStateException("HTTP ${connection.responseCode}")
            return JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
        } finally { connection.disconnect() }
    }
    override fun doWork(): Result {
        val c = applicationContext
        val p = NotificationSession.prefs(c)
        val uid = p.getInt("userId", 0)
        val origin = p.getString("origin", null) ?: return Result.success()
        val cookie = NotificationSession.cookie(c) ?: return Result.success()
        if (uid == 0 || !NativePush.configured(c)) return Result.success()
        return try {
            val token = com.google.android.gms.tasks.Tasks.await(FirebaseMessaging.getInstance().token,
                30, java.util.concurrent.TimeUnit.SECONDS)
            val session = exchange(origin, cookie, null, null)
            if (session.getInt("user_id") != uid || p.getInt("userId", 0) != uid || isStopped) return Result.success()
            if (!NotificationSession.current(c, uid, cookie)) return Result.retry()
            val reply = exchange(origin, cookie, session.getString("csrf"), JSONObject()
                .put("device_id", NativePush.deviceId(c)).put("token", token).put("platform", "android"))
            if (NotificationSession.current(c, uid, cookie) && !isStopped) {
                NotificationSession.registered(c, uid, cookie, reply.optBoolean("configured"))
            }
            if (p.getInt("userId", 0) == uid && !NotificationSession.current(c, uid, cookie) && !isStopped) Result.retry()
            else Result.success()
        } catch (_: Exception) { if (runAttemptCount >= 5) Result.failure() else Result.retry() }
    }
}

class DonatixMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) { NativePush.register(this, replace = true) }
    override fun onMessageReceived(message: RemoteMessage) {
        val p = NotificationSession.prefs(this)
        val owner = message.data["user_id"]?.toIntOrNull() ?: return
        val id = message.data["notification_id"]?.toLongOrNull() ?: return
        if (owner == 0 || owner != p.getInt("userId", 0) || NotificationSession.cookie(this) == null) return
        val link = message.data["link"] ?: "/panel/notifications"
        // Only internal, relative links can be opened from an FCM payload.
        val safe = if (link.startsWith("/panel/") && !link.startsWith("//")) link else "/panel/notifications"
        NotificationSession.show(this, owner, id, message.data["title"] ?: "Donatix",
            message.data["body"] ?: "Новое уведомление", safe)
    }
}
