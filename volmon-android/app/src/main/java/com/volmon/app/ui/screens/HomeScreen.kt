package com.volmon.app.ui.screens

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material.icons.filled.NotificationsActive
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Slider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.volmon.app.BuildConfig
import com.volmon.app.data.SampleStore
import com.volmon.app.data.Schedule
import com.volmon.app.ui.MonitorViewModel
import com.volmon.app.ui.components.HistoryTrendChart
import com.volmon.app.ui.components.LevelChart
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(nav: NavController, vm: MonitorViewModel = viewModel()) {
    val level by vm.level.collectAsState()
    val running by vm.isRunning.collectAsState()
    val recCount by vm.recordCount.collectAsState()
    val history by vm.history.collectAsState()
    val settings by vm.settings.collectAsState()
    // 分时段：计算当前生效时段与有效阈值（关闭分时段时 applied=false，直接用全局）
    val eff = Schedule.resolve(settings)
    val context = LocalContext.current

    // —— 历史趋势（可选时段：1h/4h/24h/自定义） ——
    var customMode by remember { mutableStateOf(false) }
    var customHours by remember { mutableStateOf(2f) }
    var rangeHours by remember { mutableStateOf(1f) }

    var histValues by remember { mutableStateOf<List<Float>>(emptyList()) }
    var histStart by remember { mutableStateOf(0L) }
    var histEnd by remember { mutableStateOf(0L) }
    var histAvg by remember { mutableStateOf(0f) }
    var histMax by remember { mutableStateOf(0f) }
    var histOver by remember { mutableStateOf(0f) }

    LaunchedEffect(rangeHours) {
        while (isActive) {
            val pts = SampleStore.readRange(context, rangeHours)
            if (pts.isEmpty()) {
                histValues = emptyList()
            } else {
                val now = System.currentTimeMillis()
                val start = now - (rangeHours * 3600_000L).toLong()
                val nb = 240 // 最多 240 个桶，24h 时每桶 6 分钟均值
                val bucketMs = (now - start).coerceAtLeast(1L) / nb
                val sums = DoubleArray(nb)
                val cnts = IntArray(nb)
                for (p in pts) {
                    val idx = ((p.t - start) / bucketMs).toInt().coerceIn(0, nb - 1)
                    sums[idx] += p.db
                    cnts[idx]++
                }
                histValues = (0 until nb).filter { cnts[it] > 0 }
                    .map { (sums[it] / cnts[it]).toFloat() }
                histStart = start
                histEnd = now
                histAvg = pts.map { it.db }.average().toFloat()
                histMax = pts.maxOf { it.db }
                histOver = pts.count { it.db > eff.settings.thresholdDb } * 100f / pts.size
            }
            delay(60_000) // 每分钟自动刷新
        }
    }


    // —— 运行时权限 ——
    // Android 14 起麦克风前台服务必须先拿到 RECORD_AUDIO，否则 startForeground 直接抛
    // SecurityException 导致"一点开始监测就退出"。这里先请求、授权后再启动服务。
    val notifLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { vm.start() }

    val micLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            val needNotif = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                    ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
                    PackageManager.PERMISSION_GRANTED
            if (needNotif) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) else vm.start()
        } else {
            Toast.makeText(context, "需要麦克风权限才能监测，请在系统设置中开启", Toast.LENGTH_LONG).show()
        }
    }

    fun startWithPermission() {
        val micGranted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        if (!micGranted) {
            micLauncher.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        val needNotif = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED
        if (needNotif) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) else vm.start()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("PIP-Boy")
                        Text(
                            "v${BuildConfig.VERSION_NAME}",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.outline
                        )
                    }
                },
                actions = {
                    IconButton(onClick = { nav.navigate("recordings") }) {
                        Icon(Icons.Filled.LibraryMusic, contentDescription = "录音回放")
                    }
                    IconButton(onClick = { nav.navigate("alarms") }) {
                        Icon(Icons.Filled.NotificationsActive, contentDescription = "报警记录")
                    }
                    IconButton(onClick = { nav.navigate("calibrate") }) {
                        Icon(Icons.Filled.Tune, contentDescription = "校准")
                    }
                    IconButton(onClick = { nav.navigate("settings") }) {
                        Icon(Icons.Filled.Settings, contentDescription = "设置")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                "%.1f".format(level),
                fontSize = 64.sp,
                fontWeight = FontWeight.Bold,
                color = if (level >= eff.settings.thresholdDb) Color(0xFFFF5252) else MaterialTheme.colorScheme.primary
            )
            Text("dB SPL", fontSize = 16.sp, color = Color.Gray)
            Spacer(Modifier.height(12.dp))
            LevelChart(history = history, threshold = eff.settings.thresholdDb, modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(4.dp))
            // 统计行（同 Web 版趋势图下方的 trendStat）
            val avgDb = if (history.isEmpty()) 0f else history.average().toFloat()
            val maxDb = history.maxOrNull() ?: 0f
            val overPct = if (history.isEmpty()) 0f
                else history.count { it > eff.settings.thresholdDb } * 100f / history.size
            Text(
                "均值 %.1f dB · 峰值 %.1f dB · 超阈值占比 %.1f%%".format(avgDb, maxDb, overPct),
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.Center) {
                AssistChip(onClick = {}, label = { Text(if (running) "监测中" else "已停止") })
                Spacer(Modifier.width(8.dp))
                AssistChip(onClick = {}, label = { Text("录音 %d".format(recCount)) })
                Spacer(Modifier.width(8.dp))
                AssistChip(onClick = {}, label = { Text("校准 %+.1f".format(settings.calibOffsetDb)) })
                if (eff.applied) {
                    Spacer(Modifier.width(8.dp))
                    AssistChip(onClick = {}, label = { Text("当前：${eff.periodLabel}策略") })
                }
            }
            Spacer(Modifier.height(20.dp))
            Button(
                onClick = { if (running) vm.stop() else startWithPermission() },
                modifier = Modifier.fillMaxWidth().height(52.dp)
            ) {
                Text(if (running) "停止监测" else "开始监测")
            }
            Spacer(Modifier.height(8.dp))
            Text(
                "首次点「开始监测」会请求麦克风权限；持续后台监测请保持本 App 在前台或加入电池白名单。",
                fontSize = 12.sp, color = Color.Gray
            )

            Spacer(Modifier.height(24.dp))
            Text("历史趋势", fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(1 to "1小时", 4 to "4小时", 24 to "24小时").forEach { (h, label) ->
                    FilterChip(
                        selected = !customMode && rangeHours == h.toFloat(),
                        onClick = {
                            customMode = false
                            rangeHours = h.toFloat()
                        },
                        label = { Text(label) }
                    )
                }
                FilterChip(
                    selected = customMode,
                    onClick = { customMode = true },
                    label = { Text("自定义") }
                )
            }
            if (customMode) {
                Spacer(Modifier.height(2.dp))
                Text(
                    "自定义时段：%.0f 小时".format(customHours),
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Slider(
                    value = customHours,
                    onValueChange = {
                        customHours = it
                        rangeHours = it
                    },
                    valueRange = 1f..72f
                )
            }
            Spacer(Modifier.height(6.dp))
            if (histValues.isEmpty()) {
                Text(
                    "暂无历史数据：监测运行期间每 5 秒记录一个样本，运行一段时间后即可查看。",
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.outline
                )
            } else {
                HistoryTrendChart(
                    values = histValues,
                    threshold = settings.thresholdDb,
                    startTimeMs = histStart,
                    endTimeMs = histEnd,
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "均值 %.1f dB · 峰值 %.1f dB · 超阈值占比 %.1f%%".format(histAvg, histMax, histOver),
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}
