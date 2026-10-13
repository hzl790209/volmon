pluginManagement {
    repositories {
        // 阿里云 Maven 镜像（完整同步 Maven Central，含 kotlin-compiler-embeddable，不走 github）
        maven { url = uri("https://maven.aliyun.com/repository/public") }
        // Google Maven（AndroidX 等），用 dl.google.com 直链，绕开被墙的 maven.google.com
        maven { url = uri("https://dl.google.com/dl/android/maven2") }
        // Gradle 插件门户（AGP / Kotlin Gradle 插件）
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven { url = uri("https://maven.aliyun.com/repository/public") }
        maven { url = uri("https://dl.google.com/dl/android/maven2") }
    }
}

rootProject.name = "volmon-android"
include(":app")
