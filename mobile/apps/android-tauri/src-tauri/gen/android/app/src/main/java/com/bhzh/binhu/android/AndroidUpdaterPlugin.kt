package com.bhzh.binhu.android

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin
import java.io.File
import java.security.MessageDigest

@InvokeArg
class ApkPathArgs {
  lateinit var path: String
}

@TauriPlugin
class AndroidUpdaterPlugin(private val activity: Activity) : Plugin(activity) {
  @Command
  fun getAppInfo(invoke: Invoke) {
    try {
      @Suppress("DEPRECATION")
      val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        activity.packageManager.getPackageInfo(
          activity.packageName,
          PackageManager.PackageInfoFlags.of(PackageManager.GET_SIGNING_CERTIFICATES.toLong()),
        )
      } else {
        activity.packageManager.getPackageInfo(activity.packageName, packageInfoFlags())
      }
      invoke.resolve(toAppInfo(info))
    } catch (error: Exception) {
      invoke.reject(error.message ?: "Unable to inspect the installed Android application")
    }
  }

  @Command
  fun inspectApk(invoke: Invoke) {
    try {
      val path = validatedCacheApk(invoke.parseArgs(ApkPathArgs::class.java).path)
      @Suppress("DEPRECATION")
      val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        activity.packageManager.getPackageArchiveInfo(
          path.absolutePath,
          PackageManager.PackageInfoFlags.of(PackageManager.GET_SIGNING_CERTIFICATES.toLong()),
        )
      } else {
        activity.packageManager.getPackageArchiveInfo(path.absolutePath, packageInfoFlags())
      } ?: throw IllegalArgumentException("Android cannot read the downloaded APK")
      invoke.resolve(toAppInfo(info))
    } catch (error: Exception) {
      invoke.reject(error.message ?: "Unable to inspect the downloaded APK")
    }
  }

  @Command
  fun canInstallPackages(invoke: Invoke) {
    val result = JSObject()
    result.put(
      "allowed",
      Build.VERSION.SDK_INT < Build.VERSION_CODES.O || activity.packageManager.canRequestPackageInstalls(),
    )
    invoke.resolve(result)
  }

  @Command
  fun requestInstallPermission(invoke: Invoke) {
    try {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !activity.packageManager.canRequestPackageInstalls()) {
        val intent = Intent(
          Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
          Uri.parse("package:${activity.packageName}"),
        )
        activity.startActivity(intent)
      }
      invoke.resolve()
    } catch (error: Exception) {
      invoke.reject(error.message ?: "Unable to open Android install permission settings")
    }
  }

  @Command
  fun installApk(invoke: Invoke) {
    try {
      val path = validatedCacheApk(invoke.parseArgs(ApkPathArgs::class.java).path)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !activity.packageManager.canRequestPackageInstalls()) {
        throw SecurityException("Install unknown apps permission is required")
      }
      val uri = FileProvider.getUriForFile(
        activity,
        "${activity.packageName}.fileprovider",
        path,
      )
      val intent = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, "application/vnd.android.package-archive")
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
      }
      activity.startActivity(intent)
      invoke.resolve()
    } catch (error: Exception) {
      invoke.reject(error.message ?: "Unable to open the Android package installer")
    }
  }

  private fun validatedCacheApk(value: String): File {
    val file = File(value).canonicalFile
    val cache = activity.cacheDir.canonicalFile
    if (!file.path.startsWith(cache.path + File.separator) || !file.isFile || !file.name.endsWith(".apk")) {
      throw SecurityException("APK path is outside the application update cache")
    }
    return file
  }

  @Suppress("DEPRECATION")
  private fun packageInfoFlags(): Int = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
    PackageManager.GET_SIGNING_CERTIFICATES
  } else {
    PackageManager.GET_SIGNATURES
  }

  @Suppress("DEPRECATION")
  private fun toAppInfo(info: PackageInfo): JSObject {
    val signatures = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
      info.signingInfo?.apkContentsSigners ?: emptyArray()
    } else {
      info.signatures ?: emptyArray()
    }
    if (signatures.size != 1) {
      throw SecurityException("Exactly one Android APK signer is required")
    }
    val digest = MessageDigest.getInstance("SHA-256").digest(signatures[0].toByteArray())
    val result = JSObject()
    result.put("packageName", info.packageName)
    result.put("versionName", info.versionName ?: "")
    result.put(
      "versionCode",
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) info.longVersionCode else info.versionCode.toLong(),
    )
    result.put("signerSha256", digest.joinToString("") { byte -> "%02x".format(byte) })
    return result
  }
}
