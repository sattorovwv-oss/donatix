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
    fun configure(c: Context, origin: String, cookie: String, userId: Int) {
        val uri = URL(origin)
        require(uri.protocol == "https" && uri.host.isNotBlank() && uri.userInfo == null)
        val p = prefs(c)
        if (p.getInt("userId", 0) != userId) p.edit().clear().apply()
        storeCookie(c, cookie)
        p.edit().putString("origin", origin).putInt("userId", userId).apply()
        val constraints = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
        val work = PeriodicWorkRequestBuilder<NotificationWorker>(15, TimeUnit.MINUTES).setConstraints(constraints).build()
        WorkManager.getInstance(c).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.UPDATE, work)
        if (!p.contains("watermark")) WorkManager.getInstance(c).enqueueUniqueWork("donatix-inbox-seed", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<NotificationWorker>().setConstraints(constraints).build())
    }
    fun stop(c: Context) {
        WorkManager.getInstance(c).cancelUniqueWork(WORK)
        WorkManager.getInstance(c).cancelUniqueWork("donatix-inbox-seed")
        prefs(c).edit().clear().apply()
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
                if (p.getInt("userId", 0) == uid && NotificationSession.cookie(c) == session) NotificationSession.stop(c)
                return Result.success()
            }
            if (connection.responseCode != 200) return Result.retry()
            val payload = connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
            if (payload.length > 1024 * 1024) return Result.failure()
            if (p.getInt("userId", 0) != uid || NotificationSession.cookie(c) == null) return Result.success()
            val rows = JSONObject(payload).getJSONArray("items")
            val initialized = p.contains("watermark")
            val before = p.getLong("watermark", 0)
            var latest = before
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) c.getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel("donatix-orders", "Заказы и баланс Donatix", NotificationManager.IMPORTANCE_DEFAULT))
            val allowed = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            for (i in rows.length() - 1 downTo 0) {
                if (isStopped || p.getInt("userId", 0) != uid) return Result.success()
                val row = rows.getJSONObject(i)
                val id = row.getLong("id")
                latest = maxOf(latest, id)
                if (!initialized || id <= before || !allowed || !row.isNull("read_at")) continue
                val intent = Intent(c, MainActivity::class.java).putExtra("donatix_link", row.optString("link", "/panel/notifications"))
                    .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                val pending = PendingIntent.getActivity(c, id.toInt(), intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                val body = row.optString("text", "")
                val notification = NotificationCompat.Builder(c, "donatix-orders").setSmallIcon(R.drawable.ic_notification)
                    .setContentTitle(row.optString("title", "").ifBlank { "Donatix" }).setContentText(body)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(body)).setContentIntent(pending).setAutoCancel(true).build()
                NotificationManagerCompat.from(c).notify(id.toInt(), notification)
            }
            if (p.getInt("userId", 0) == uid) p.edit().putLong("watermark", latest).apply()
            Result.success()
        } catch (_: Exception) { Result.retry() }
        finally { connection?.disconnect() }
    }
}
