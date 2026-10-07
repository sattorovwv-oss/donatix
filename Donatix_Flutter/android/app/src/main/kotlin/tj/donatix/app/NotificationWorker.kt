package tj.donatix.app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.security.KeyStore
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

object NotificationSession {
    private const val ALIAS = "donatix.notification.session"
    private const val WORK = "donatix-inbox"
    fun prefs(c: Context) = c.getSharedPreferences("donatix-native-notifications", Context.MODE_PRIVATE)
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    fun storeCookie(c: Context, cookie: String) {
        storeSecret(c, "cookie", "iv", cookie)
    }
    private fun storeSecret(c: Context, name: String, ivName: String, value: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        prefs(c).edit().putString(name, Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .putString(ivName, Base64.encodeToString(cipher.iv, Base64.NO_WRAP)).apply()
    }
    fun cookie(c: Context): String? = secret(c, "cookie", "iv")
    fun apiKey(c: Context): String? = secret(c, "rest_key", "rest_key_iv")
    private fun secret(c: Context, name: String, ivName: String): String? {
        val p = prefs(c)
        val raw = p.getString(name, null) ?: return null
        val iv = p.getString(ivName, null) ?: return null
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP))) }
            String(cipher.doFinal(Base64.decode(raw, Base64.NO_WRAP)), Charsets.UTF_8)
        } catch (_: Exception) { null }
    }
    private fun sessionIdentity(value: String?): String? {
        if (value == null) return null
        // Starlette refreshes the signed cookie timestamp on normal responses.
        // The server-side sid stays constant until logout/new login. Comparing
        // the entire cookie would repeatedly invalidate in-flight registration.
        return try {
            val payload = String(Base64.decode(value.substringBefore('.'), Base64.DEFAULT), Charsets.UTF_8)
            JSONObject(payload).optString("sid").takeIf { it.isNotBlank() }?.let { "sid:$it" } ?: value
        } catch (_: Exception) { value }
    }
    fun current(c: Context, uid: Int, expectedCookie: String): Boolean =
        prefs(c).getInt("userId", 0) == uid && sessionIdentity(cookie(c)) == sessionIdentity(expectedCookie)
    @Synchronized
    fun registered(c: Context, uid: Int, expectedCookie: String, server: Boolean) {
        if (current(c, uid, expectedCookie)) prefs(c).edit().putBoolean("push_registered", true).putBoolean("push_server", server).apply()
    }
    @Synchronized
    fun watermark(c: Context, uid: Int, expectedCookie: String, value: Long) {
        if (current(c, uid, expectedCookie)) prefs(c).edit().putLong("watermark", value).apply()
    }
    @Synchronized
    fun configure(c: Context, origin: String, cookie: String, userId: Int,
                  existingApi: Boolean = false, personalKey: String? = null) {
        val uri = URL(origin)
        require(uri.protocol == "https" && uri.host.isNotBlank() && uri.userInfo == null &&
            uri.query == null && uri.ref == null && (uri.path.isEmpty() || uri.path == "/") &&
            userId > 0 && cookie.isNotBlank())
        require(!existingApi || !personalKey.isNullOrBlank())
        val p = prefs(c)
        val changed = p.getInt("userId", 0) != userId || p.getString("origin", null) != origin ||
            sessionIdentity(cookie(c)) != sessionIdentity(cookie) ||
            p.getBoolean("existing_api", false) != existingApi ||
            (existingApi && apiKey(c) != personalKey)
        if (p.getInt("userId", 0) != userId) {
            p.edit().clear().apply()
            NotificationManagerCompat.from(c).cancelAll()
        }
        if (changed) p.edit().putBoolean("push_registered", false).putBoolean("push_server", false).apply()
        storeCookie(c, cookie)
        if (existingApi) storeSecret(c, "rest_key", "rest_key_iv", personalKey!!)
        else p.edit().remove("rest_key").remove("rest_key_iv").apply()
        p.edit().putString("origin", origin).putInt("userId", userId)
            .putBoolean("existing_api", existingApi).apply()
        if (changed) p.edit().remove("rest_snapshot").apply()
        if (existingApi) NativePush.stop(c) else NativePush.register(c, replace = changed)
        val constraints = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
        val work = PeriodicWorkRequestBuilder<NotificationWorker>(15, TimeUnit.MINUTES).setConstraints(constraints).build()
        WorkManager.getInstance(c).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.UPDATE, work)
        if (!p.contains("watermark")) WorkManager.getInstance(c).enqueueUniqueWork("donatix-inbox-seed", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<NotificationWorker>().setConstraints(constraints).build())
    }
    @Synchronized
    fun show(c: Context, uid: Int, id: Long, title: String, body: String, link: String, expectedCookie: String? = null) {
        val p = prefs(c)
        if (p.getInt("userId", 0) != uid || (expectedCookie != null && !current(c, uid, expectedCookie))) return
        if (!NotificationManagerCompat.from(c).areNotificationsEnabled()) return
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val seen = (p.getStringSet("shown_ids", emptySet()) ?: emptySet()).toMutableSet()
        if (seen.contains(id.toString())) return
        if (Build.VERSION.SDK_INT >= 26) c.getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel("donatix-orders", "Заказы и баланс Donatix", NotificationManager.IMPORTANCE_DEFAULT))
        val safeLink = if (link.startsWith("/panel/") && !link.startsWith("//") && !link.contains('\\')) link else "/panel/notifications"
        val intent = Intent(c, MainActivity::class.java).putExtra("donatix_link", safeLink).putExtra("donatix_user", uid)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val pending = PendingIntent.getActivity(c, id.toInt(), intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(c, "donatix-orders").setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title).setContentText(body).setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(pending).setAutoCancel(true).build()
        NotificationManagerCompat.from(c).notify(id.toInt(), notification)
        seen.add(id.toString())
        p.edit().putStringSet("shown_ids", seen.sortedByDescending { it.toLongOrNull() ?: 0 }.take(200).toSet()).apply()
    }
    @Synchronized
    fun stop(c: Context) {
        // Remove the session first: cancelled workers and pending FCM callbacks
        // must not write registration state or display a signed-out account.
        prefs(c).edit().clear().apply()
        NativePush.stop(c)
        WorkManager.getInstance(c).cancelUniqueWork(WORK)
        WorkManager.getInstance(c).cancelUniqueWork("donatix-inbox-seed")
        NotificationManagerCompat.from(c).cancelAll()
    }
}

class NotificationWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        val c = applicationContext
        val p = NotificationSession.prefs(c)
        val session = NotificationSession.cookie(c) ?: return Result.success()
        val uid = p.getInt("userId", 0)
        val origin = p.getString("origin", null) ?: return Result.success()
        if (p.getBoolean("existing_api", false)) return checkExisting(c, uid, session, origin)
        var connection: HttpURLConnection? = null
        return try {
            connection = URL(origin.trimEnd('/') + "/api/v1/mobile/notifications").openConnection() as HttpURLConnection
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 15000; connection.readTimeout = 20000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Cookie", "dx_session=$session")
            connection.setRequestProperty("User-Agent", "DonatixNativeNotifications/1.0")
            if (connection.responseCode in listOf(401, 403)) {
                if (NotificationSession.current(c, uid, session)) NotificationSession.stop(c)
                return Result.success()
            }
            if (connection.responseCode != 200) return Result.retry()
            val payload = connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
            if (payload.length > 1024 * 1024) return Result.failure()
            if (!NotificationSession.current(c, uid, session) || isStopped) return Result.success()
            val rows = JSONObject(payload).getJSONArray("items")
            val initialized = p.contains("watermark")
            val before = p.getLong("watermark", 0)
            var latest = before
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) c.getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel("donatix-orders", "Заказы и баланс Donatix", NotificationManager.IMPORTANCE_DEFAULT))
            val allowed = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            for (i in rows.length() - 1 downTo 0) {
                if (isStopped || !NotificationSession.current(c, uid, session)) return Result.success()
                val row = rows.getJSONObject(i)
                val id = row.getLong("id")
                latest = maxOf(latest, id)
                if (!initialized || id <= before || !allowed || !row.isNull("read_at")) continue
                NotificationSession.show(c, uid, id, row.optString("title", "").ifBlank { "Donatix" },
                    row.optString("text", ""), row.optString("link", "/panel/notifications"))
            }
            if (!isStopped) NotificationSession.watermark(c, uid, session, latest)
            Result.success()
        } catch (_: Exception) { Result.retry() }
        finally { connection?.disconnect() }
    }

    private fun checkExisting(c: Context, uid: Int, session: String, origin: String): Result {
        val p = NotificationSession.prefs(c)
        val key = NotificationSession.apiKey(c) ?: return Result.success()
        fun current() = !isStopped && NotificationSession.current(c, uid, session) &&
            NotificationSession.apiKey(c) == key
        fun read(path: String, json: Boolean = true): String? {
            val connection = URL(origin.trimEnd('/') + path).openConnection() as HttpURLConnection
            try {
                connection.instanceFollowRedirects = false
                connection.connectTimeout = 15000; connection.readTimeout = 20000
                connection.setRequestProperty("Cookie", "dx_session=$session; dx_cur=USD")
                connection.setRequestProperty("Accept", if (json) "application/json" else "text/html")
                if (json) connection.setRequestProperty("X-API-Key", key)
                if (connection.responseCode in listOf(401, 403) ||
                    (!json && connection.responseCode in 300..399)) {
                    if (current()) NotificationSession.stop(c)
                    return null
                }
                if (connection.responseCode != 200) throw IllegalStateException("HTTP error")
                val bytes = connection.inputStream.use { input ->
                    val output = java.io.ByteArrayOutputStream()
                    val buffer = ByteArray(8192)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        require(output.size() + count <= 1024 * 1024)
                        output.write(buffer, 0, count)
                    }
                    output.toByteArray()
                }
                return String(bytes, Charsets.UTF_8)
            } finally { connection.disconnect() }
        }
        return try {
            // Validate the browser session too: a revoked session must never
            // leave its otherwise still-valid personal API key polling.
            val panel = read("/panel", false) ?: return Result.success()
            if (!panel.contains("action=\"/logout\"")) return Result.retry()
            val orders = JSONObject(read("/api/v1/orders?limit=100") ?: return Result.success())
            val payments = JSONObject(read("/api/v1/payments?limit=100") ?: return Result.success())
            val balance = JSONObject(read("/api/v1/balance") ?: return Result.success())
            if (!current()) return Result.success()
            val next = JSONObject().put("balance", balance.getString("balance"))
            for ((label, payload) in listOf("order" to orders, "payment" to payments)) {
                val rows = payload.getJSONArray("items")
                for (i in 0 until rows.length()) {
                    val row = rows.getJSONObject(i)
                    val id = row.get(if (label == "order") "order_id" else "id").toString()
                    next.put("$label:$id", row.getString("status"))
                }
            }
            val before = p.getString("rest_snapshot", null)?.let { JSONObject(it) }
            val revision = p.getLong("rest_revision", 0) + 1
            if (before != null) {
                for (entry in next.keys()) {
                    if (!current()) return Result.success()
                    val value = next.getString(entry)
                    if (value == before.optString(entry)) continue
                    val order = entry.startsWith("order:")
                    val status = when (value) {
                        "completed", "approved" -> "Выполнено"
                        "failed", "rejected" -> "Отклонено"
                        "processing", "attention" -> "Обрабатывается"
                        "pending", "waiting" -> "Ожидает оплаты"
                        else -> value
                    }
                    val title = if (entry == "balance") "Баланс Donatix"
                        else if (order) "Заказ " + entry.substringAfter(':') else "Пополнение Donatix"
                    val body = if (entry == "balance") "Текущий баланс: " + value + " USD" else status
                    val link = if (order) "/panel/orders/" + entry.substringAfter(':') else "/panel/balance"
                    val eventId = (entry + ":" + value + ":" + revision).hashCode().toLong() and 0xffffffffL
                    NotificationSession.show(c, uid, eventId, title, body, link, session)
                }
            }
            synchronized(NotificationSession) {
                if (current()) p.edit().putString("rest_snapshot", next.toString()).putLong("rest_revision", revision).apply()
            }
            Result.success()
        } catch (_: Exception) { Result.retry() }
    }
}
