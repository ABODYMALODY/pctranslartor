plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.gametranslator.tv"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.gametranslator.tv"
        minSdk = 26 // drawing over other apps (TYPE_APPLICATION_OVERLAY) needs Android 8
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed with the debug key so the APK installs on your own TV without extra setup.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

// No libraries: only the Android framework and Kotlin, so there is little that can break.
dependencies {}
