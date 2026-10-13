package com.volmon.app.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.volmon.app.R
import com.volmon.app.audio.AlarmSound
import com.volmon.app.audio.AudioCapture
import com.volmon.app.data.AlarmStore
import com.volmon.app.data.AudioLevel
import com.volmon.app.data.MonitorState
import com.volmon.app.data.Preferences
import com.volmon.app.data.RecordingStore
import com.volmon.app.data.SampleStore
import com.volmon.app.data.Settings
import com.volmon.app.data.Schedule
import com.volmon.app.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 前台监测服务：持有 AudioCapture，按阈值/持续判定触发报警声与录音，
 * 并把实时 dB 推到 MonitorState 供 UI 显示。
 *
 * 前台服务 + 通知是 Android 后台持续采集音频的唯一合规方式（省电/Doze 仍可能限制，
 * 但比普通后台服务持久得多）。锁屏后系统仍可能节流，介意的话可引导用户关闭电池优化。
 */
class MonitoringService : Service() {
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    private lateinit var audio: AudioCapture
    private lateinit var alarm: AlarmSound
    private lateinit var prefs: Preferences
    @Volatile
    private var settings: Settings = Settings()

    private var aboveSince = 0L
    private var lastAlarmEnd = 0L
    private var lastSampleAt = 0L
    private var capturingPost = false
    private val postChunks = mutableListOf<ShortArray>()

    /**
     * 报警声"污染窗口"截止时间戳：报警声从扬声器播出会被本机麦克风拾入（自发自收），
     * 播放期间 + 短衰减期内的采集数据视为被污染——不写录音、不更新实时 dB/曲线、不参与触发判定。
     */
    @Volatile
    private var pollutionUntil = 0L

    override fun onCreate() {
        super.onCreate()
        prefs = Preferences(this)
        alarm = AlarmSound()
        audio = AudioCapture()
        audio.isContaminated = { System.currentTimeMillis() < pollutionUntil }
        audio.onChunk = { chunk ->
            // 污染窗口内的 PCM 丢弃，避免报警蜂鸣声混进录音
            if (capturingPost && System.currentTimeMillis() >= pollutionUntil) {
                synchronized(postChunks) { postChunks.add(chunk) }
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        createChannel()
        val fst = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
            android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        ServiceCompat.startForeground(this, NOTIF_ID, buildNotification(AudioLevel.DBFS_FLOOR), fst)
        MonitorState.setRunning(true)
        MonitorState.resetHistory()
        scope.launch {
            try {
                settings = prefs.settings.first()
                // 持续同步最新设置（页面改了即时生效，无需重启服务）
                launch { prefs.settings.collect { settings = it } }
                audio.start()
                observe()
            } catch (e: Exception) {
                // 采集初始化失败（麦克风被占用/无设备等）：优雅退出，不让进程崩溃
                MonitorState.setRunning(false)
                stopSelf()
            }
        }
        return START_STICKY
    }

    private suspend fun observe() {
        audio.level.collect { dbfs ->
            // 污染窗口：报警声 playback 期间读数是"自己听到自己"，整体丢弃
            if (System.currentTimeMillis() < pollutionUntil) return@collect
            // 分时段：按时段算出"有效配置"，覆盖阈值/持续/报警三项（录音相关仍用全局 settings）
            val eff = Schedule.resolve(settings)
            MonitorState.setPeriodLabel(if (eff.applied) eff.periodLabel else "")
            val s = eff.settings
            val spl = AudioLevel.dbfsToSpl(dbfs, settings.calibOffsetDb)
            MonitorState.setLevel(spl)
            MonitorState.setRaw(dbfs)
            updateNotification(spl)
            // 历史采样：每 5 秒落盘一次（供主页 1h/4h/24h/自定义趋势图）
            val nowS = System.currentTimeMillis()
            if (nowS - lastSampleAt >= SAMPLE_INTERVAL_MS) {
                lastSampleAt = nowS
                SampleStore.append(this@MonitoringService, spl)
            }
            val now = System.currentTimeMillis()
            if (spl >= s.thresholdDb) {
                if (aboveSince == 0L) aboveSince = now
                if ((now - aboveSince) / 1000f >= s.sustainSec) {
                    triggerAlarm(spl)
                    aboveSince = 0L
                }
            } else {
                aboveSince = 0L
            }
        }
    }

    private fun triggerAlarm(spl: Float) {
        val now = System.currentTimeMillis()
        // 分时段：报警冷却/开关/时长取"有效配置"
        val s = Schedule.resolve(settings).settings
        if (now - lastAlarmEnd < s.alarmCooldownSec * 1000) return
        if (s.alarmEnabled) {
            val dev = resolveAlarmDevice()
            alarm.beep(s.alarmDurationSec, dev)
            pollutionUntil = now + (s.alarmDurationSec * 1000).toLong() + POLLUTION_DECAY_MS
        } else {
            pollutionUntil = 0L
        }
        lastAlarmEnd = now + (s.alarmDurationSec * 1000).toLong()
        MonitorState.noteAlarm()
        AlarmStore.log(this, spl)
        if (settings.recEnabled) startRecording()
    }

    /**
     * 把设置里保存的输出设备解析成 [AudioDeviceInfo]。
     * 默认（id=0 且无地址）返回 null → 报警走系统默认路由；
     * 否则按地址（蓝牙更稳）优先、id 兜底，在已连接输出设备里查找。
     */
    private fun resolveAlarmDevice(): AudioDeviceInfo? {
        val id = settings.alarmDeviceId
        val addr = settings.alarmDeviceAddr
        if (id == 0 && addr.isEmpty()) return null
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val outputs = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
        return outputs.firstOrNull { it.address.isNotEmpty() && it.address == addr }
            ?: outputs.firstOrNull { it.id == id }
    }

    private fun startRecording() {
        val pre = audio.pullRing(settings.recPreSec)
        synchronized(postChunks) { postChunks.clear() }
        capturingPost = true
        scope.launch {
            delay((settings.recPostSec * 1000).toLong())
            capturingPost = false
            val post = synchronized(postChunks) { postChunks.toList() }
            synchronized(postChunks) { postChunks.clear() }
            val all = concatenate(pre, post)
            if (all.isNotEmpty()) {
                if (saveWav(all)) MonitorState.bumpRecord()
            }
        }
    }

    private fun concatenate(a: ShortArray, b: List<ShortArray>): ShortArray {
        var total = a.size
        for (x in b) total += x.size
        val out = ShortArray(total)
        System.arraycopy(a, 0, out, 0, a.size)
        var off = a.size
        for (x in b) {
            System.arraycopy(x, 0, out, off, x.size)
            off += x.size
        }
        return out
    }

    /**
     * 落盘录音。落盘前先按"录音数量/存储空间"限制腾空间：
     * 覆盖策略 → 删最旧的；停止存储策略 → 直接放弃（返回 false）。
     * @return true 表示已写入，调用方可计入录音数。
     */
    private fun saveWav(data: ShortArray): Boolean {
        val dir = RecordingStore.dir(this)
        val name = SimpleDateFormat("yyyy-MM-dd_HH-mm-ss", Locale.US).format(Date())
        val file = File(dir, "$name.wav")
        // 16-bit mono：44 字节头 + 每采样 2 字节
        val incoming = 44L + data.size.toLong() * 2L
        val allowed = RecordingStore.makeRoom(
            this, incoming, settings.recMaxCount, settings.recMaxStorageMb, settings.recOverflowPolicy
        )
        if (!allowed) return false
        WavWriter.write(file, data, 44100)
        // 同名覆盖（同一秒内多次触发）时不会新增，此处再裁剪一次兜底
        RecordingStore.prune(this, settings.recMaxCount, settings.recMaxStorageMb)
        return true
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID, getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            )
            ch.description = getString(R.string.notification_channel_desc)
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(spl: Float): Notification {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("volmon 监测中")
            .setContentText("当前 %.1f dB".format(spl))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(spl: Float) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIF_ID, buildNotification(spl))
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        audio.stop()
        alarm.release()
        MonitorState.setRunning(false)
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        const val NOTIF_ID = 1
        const val CHANNEL_ID = "volmon_monitor"

        /** 报警声停后额外压制的毫秒数（覆盖房间混响/扬声器余音）。 */
        const val POLLUTION_DECAY_MS = 1500L

        /** 历史采样落盘间隔。 */
        const val SAMPLE_INTERVAL_MS = 5000L
    }
}
