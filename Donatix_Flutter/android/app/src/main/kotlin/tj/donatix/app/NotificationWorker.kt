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
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val encrypted = cipher.doFinal(cookie.toByteArray(Charsets.UTF_8))
        prefs(c).edit().putString("cookie", Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP)).apply()
    }
    fun cookie(c: Context): String? {
        val p = prefs(c)
        val raw = p.getString("cookie", null) ?: return null
        val iv = p.getString("iv", null) ?: return null
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
    fun configure(c: Context, origin: String, cookie: String, userId: Int) {
        val uri = URL(origin)
        require(uri.protocol == "https" && uri.host.isNotBlank() && uri.userInfo == null &&
            uri.query == null && uri.ref == null && (uri.path.isEmpty() || uri.path == "/") &&
            userId > 0 && cookie.isNotBlank())
        val p = prefs(c)
        val changed = p.getInt("userId", 0) != userId || p.getString("origin", null) != origin ||
            sessionIdentity(cookie(c)) != sessionIdentity(cookie)
        if (p.getInt("userId", 0) != userId) {
            p.edit().clear().apply()
            NotificationManagerCompat.from(c).cancelAll()
        }
        if (changed) p.edit().putBoolean("push_registered", false).putBoolean("push_server", false).apply()
        storeCookie(c, cookie)
        p.edit().putString("origin", origin).putInt("userId", userId).apply()
        NativePush.register(c, replace = changed)
        val constraints = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
        val work = PeriodicWorkRequestBuilder<NotificationWorker>(15, TimeUnit.MINUTES).setConstraints(constraints).build()
        WorkManager.getInstance(c).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.UPDATE, work)
        if (!p.contains("watermark")) WorkManager.getInstance(c).enqueueUniqueWork("donatix-inbox-seed", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<NotificationWorker>().setConstraints(constraints).build())
    }
    @Synchronized
    fun show(c: Context, uid: Int, id: Long, title: String, body: String, link: String) {
        val p = prefs(c)
        if (p.getInt("userId", 0) != uid) return
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
}
