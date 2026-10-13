package com.volmon.app.ui

import android.app.Application
import android.content.Intent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.volmon.app.data.AudioLevel
import com.volmon.app.data.MonitorState
import com.volmon.app.data.Preferences
import com.volmon.app.data.Settings
import com.volmon.app.service.MonitoringService
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * UI 与后台的桥：启停前台服务、读设置、改设置、校准。
 */
class MonitorViewModel(private val app: Application) : AndroidViewModel(app) {
    private val prefs = Preferences(app)

    val settings = prefs.settings.stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5000), Settings()
    )
    val level = MonitorState.level
    val isRunning = MonitorState.isRunning
    val recordCount = MonitorState.recordCount
    val history = MonitorState.history
    val rawDbfs = MonitorState.rawDbfs

    fun start() {
        app.startForegroundService(Intent(app, MonitoringService::class.java))
    }

    fun stop() {
        app.stopService(Intent(app, MonitoringService::class.java))
    }

    /** 写入设置（不可变更新，避免散落一堆 setter）。 */
    fun set(block: (Settings) -> Settings) {
        viewModelScope.launch { prefs.update(block) }
    }

    /** 用专业设备实测值校准：offset = realDb − (当前 dBFS + 基准偏移)。 */
    fun calibrate(realDb: Float): Float {
        val raw = MonitorState.rawDbfs.value
        val offset = AudioLevel.computeCalibration(realDb, raw)
        set { it.copy(calibOffsetDb = offset) }
        return offset
    }
}
