package com.volmon.app.ui

import android.app.Activity
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.volmon.app.ui.components.ScanlineOverlay
import com.volmon.app.ui.screens.AlarmLogScreen
import com.volmon.app.ui.screens.CalibrationScreen
import com.volmon.app.ui.screens.HomeScreen
import com.volmon.app.ui.screens.RecordingsScreen
import com.volmon.app.ui.screens.SettingsScreen
import com.volmon.app.ui.theme.ThemeModes
import com.volmon.app.ui.theme.VolmonTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            // 主题需要先读到设置再应用
            val vm: MonitorViewModel = viewModel()
            val settings by vm.settings.collectAsState()

            VolmonTheme(themeMode = settings.themeMode) {
                // 屏幕常亮：按设置开关 FLAG_KEEP_SCREEN_ON，监测/查看时不锁屏
                val window = (LocalContext.current as? Activity)?.window
                DisposableEffect(settings.keepScreenOn) {
                    if (window != null) {
                        if (settings.keepScreenOn) {
                            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        } else {
                            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        }
                    }
                    onDispose { }
                }

                Box(Modifier.fillMaxSize()) {
                    Surface(
                        modifier = Modifier.fillMaxSize(),
                        color = MaterialTheme.colorScheme.background
                    ) {
                        AppNav(vm)
                    }
                    // PIP-Boy 主题：整屏 CRT 扫描线叠加
                    if (settings.themeMode == ThemeModes.PIP_BOY) {
                        ScanlineOverlay(Modifier.fillMaxSize())
                    }
                }
            }
        }
    }

    @Composable
    private fun AppNav(vm: MonitorViewModel) {
        val nav = rememberNavController()
        NavHost(nav, startDestination = "home") {
            // 统一用 Activity 级 vm：避免各 composable 目的地自建 nav 级 vm，
            // 返回时该 vm 被销毁 → viewModelScope 取消 → DataStore 写入未完成 → 设置不保存。
            composable("home") { HomeScreen(nav, vm) }
            composable("settings") { SettingsScreen(nav, vm) }
            composable("calibrate") { CalibrationScreen(nav, vm) }
            composable("recordings") { RecordingsScreen(nav) }
            composable("alarms") { AlarmLogScreen(nav) }
        }
    }
}
