package tj.donatix.app

import io.flutter.embedding.android.FlutterActivity
import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.FileProvider
import androidx.core.app.NotificationManagerCompat
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : FlutterActivity() {
    private var permissionReply: MethodChannel.Result? = null
    private var nativeChannel: MethodChannel? = null
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        nativeChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "tj.donatix.app/native")
        nativeChannel!!.setMethodCallHandler { call, result ->
            try {
                when (call.method) {
                    "configureNotifications" -> {
                        NotificationSession.configure(this, call.argument<String>("origin")!!, call.argument<String>("cookie")!!, call.argument<Number>("userId")!!.toInt(),
                            call.argument<Boolean>("existingApi") ?: false, call.argument<String>("apiKey"))
                        result.success(null)
                    }
                    "clearPrivateFiles" -> { File(cacheDir, "shared").deleteRecursively(); result.success(null) }
                    "pushStatus" -> { result.success(NativePush.status(this)) }
                    "stopNotifications" -> { NotificationSession.stop(this); result.success(null) }
                    "requestNotificationPermission" -> {
                        if (Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) result.success(NotificationManagerCompat.from(this).areNotificationsEnabled())
                        else if (permissionReply != null) result.error("pending", "Permission request already open", null)
                        else { permissionReply = result; requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 735) }
                    }
                    "takeNotificationLink" -> {
                        val link = intent?.getStringExtra("donatix_link")
                        val owner = intent?.getIntExtra("donatix_user", 0)
                        intent?.removeExtra("donatix_link")
                        intent?.removeExtra("donatix_user")
                        result.success(if (owner == NotificationSession.prefs(this).getInt("userId", 0)) link else null)
                    }
                    "shareFile" -> {
                        val bytes = call.argument<ByteArray>("bytes") ?: error("Missing file")
                        require(bytes.size <= 32 * 1024 * 1024)
                        val name = (call.argument<String>("name") ?: "Donatix").replace(Regex("[^A-Za-z0-9._-]"), "_")
                            .trim('.').take(180).ifBlank { "Donatix" }
                        val directory = File(cacheDir, "shared").apply { mkdirs() }
                        directory.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 86400000 }?.forEach { it.delete() }
                        val file = File(directory, name).apply { writeBytes(bytes) }
                        val uri = FileProvider.getUriForFile(this, "$packageName.files", file)
                        val share = Intent(Intent.ACTION_SEND).setType(call.argument<String>("mime") ?: "application/octet-stream")
                            .putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        startActivity(Intent.createChooser(share, "Открыть / сохранить файл"))
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            } catch (_: Exception) { result.error("native_error", "Не удалось выполнить действие Android", null) }
        }
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent); setIntent(intent)
        if (intent.hasExtra("donatix_link")) nativeChannel?.invokeMethod("notificationOpened", null)
    }
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 735) { permissionReply?.success(grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED && NotificationManagerCompat.from(this).areNotificationsEnabled()); permissionReply = null }
    }
}
