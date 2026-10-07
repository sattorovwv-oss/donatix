import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    id("dev.flutter.flutter-gradle-plugin")
}
if (file("google-services.json").exists()) apply(plugin = "com.google.gms.google-services")
val signing = Properties()
val signingFile = rootProject.file("key.properties")
if (signingFile.exists()) signingFile.inputStream().use { signing.load(it) }
val hasSigning = listOf("storeFile", "storePassword", "keyAlias", "keyPassword").all { !signing.getProperty(it).isNullOrBlank() }
if (gradle.startParameter.taskNames.any { it.contains("Release", ignoreCase = true) } && !hasSigning) {
    throw GradleException("Release signing is missing. Create android/key.properties using the owner's upload key. Debug signing is never used for releases.")
}
android {
    namespace = "tj.donatix.app"
    compileSdk = 36
    ndkVersion = flutter.ndkVersion
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = JavaVersion.VERSION_17.toString() }
    defaultConfig {
        applicationId = "tj.donatix.app"
        minSdk = 24
        targetSdk = 36
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }
    signingConfigs {
        if (hasSigning) create("release") {
            storeFile = file(signing.getProperty("storeFile"))
            storePassword = signing.getProperty("storePassword")
            keyAlias = signing.getProperty("keyAlias")
            keyPassword = signing.getProperty("keyPassword")
        }
    }
    buildTypes {
        release {
            if (hasSigning) signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = true
            isShrinkResources = true
        }
    }
}
flutter { source = "../.." }
dependencies {
    implementation(platform("com.google.firebase:firebase-bom:34.19.0"))
    implementation("com.google.firebase:firebase-messaging")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("androidx.core:core-ktx:1.16.0")
}
