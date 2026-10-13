package com.volmon.app.ui.screens

import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.volmon.app.data.Schedule
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings as AndroidSettings
import android.widget.Toast
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.TextButton
import androidx.compose.material3.TimePicker
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import android.media.AudioDeviceInfo
import android.media.AudioManager
import com.volmon.app.data.RecordingStore
import com.volmon.app.ui.MonitorViewModel
import com.volmon.app.ui.theme.ThemeModes

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(nav: NavController, vm: MonitorViewModel = viewModel()) {
    val settings by vm.settings.collectAsState()
    val context = LocalContext.current
    // 录音目录统计：进入页面时快照一次，清空后手动刷新
    var recCount by remember { mutableStateOf(RecordingStore.count(context)) }
    var recBytes by remember { mutableStateOf(RecordingStore.totalBytes(context)) }
    var confirmClear by remember { mutableStateOf(false) }

    // 报警输出设备枚举：进入页面时快照一次，可手动刷新（蓝牙连接状态会变）
    var routeRefresh by remember { mutableStateOf(0) }
    var outputs by remember { mutableStateOf<List<AudioDeviceInfo>>(emptyList()) }
    LaunchedEffect(routeRefresh) {
        val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        outputs = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).filter { it.isSink }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("设置") },
                navigationIcon = {
                    IconButton(onClick = { nav.popBackStack() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
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
                .verticalScroll(rememberScrollState())
        ) {
            // —— 主题切换 ——
            Text("主题", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Row {
                FilterChip(
                    selected = settings.themeMode == ThemeModes.HACKER,
                    onClick = { vm.set { s -> s.copy(themeMode = ThemeModes.HACKER) } },
                    label = { Text("黑客绿") },
                    colors = FilterChipDefaults.filterChipColors(
                        selectedContainerColor = MaterialTheme.colorScheme.surfaceVariant
                    )
                )
                Spacer(Modifier.width(8.dp))
                FilterChip(
                    selected = settings.themeMode == ThemeModes.PIP_BOY,
                    onClick = { vm.set { s -> s.copy(themeMode = ThemeModes.PIP_BOY) } },
                    label = { Text("PIP-Boy 终端") },
                    colors = FilterChipDefaults.filterChipColors(
                        selectedContainerColor = MaterialTheme.colorScheme.surfaceVariant
                    )
                )
            }
            Text(
                if (settings.themeMode == ThemeModes.PIP_BOY)
                    "辐射4 PIP-Boy：磷光绿 CRT、方角像素化 + 扫描线"
                else "近纯黑底 + 荧光绿",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline
            )
            Spacer(Modifier.height(16.dp))

            SliderRow("预警阈值", "%.0f dB".format(settings.thresholdDb),
                settings.thresholdDb, 40f, 110f) { vm.set { s -> s.copy(thresholdDb = it) } }
            NumberFieldRow("持续触发（秒，最小精度 0.01）", settings.sustainSec, 0f, 60f) { v ->
                vm.set { s -> s.copy(sustainSec = v) }
            }

            Spacer(Modifier.height(8.dp))
            SwitchRow("屏幕常亮（监测页面打开时不锁屏）", settings.keepScreenOn) {
                vm.set { s -> s.copy(keepScreenOn = it) }
            }

            Spacer(Modifier.height(8.dp))
            SwitchRow("超阈值播放报警声", settings.alarmEnabled) {
                vm.set { s -> s.copy(alarmEnabled = it) }
            }
            SliderRow("报警声时长", "%.0f s".format(settings.alarmDurationSec),
                settings.alarmDurationSec, 1f, 30f) { vm.set { s -> s.copy(alarmDurationSec = it) } }
            SliderRow("报警冷却", "%.0f s".format(settings.alarmCooldownSec),
                settings.alarmCooldownSec, 1f, 30f) { vm.set { s -> s.copy(alarmCooldownSec = it) } }

            // —— 报警输出设备 ——
            Spacer(Modifier.height(16.dp))
            Text("报警输出设备", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                "默认走系统提醒音路由（多数手机会强制从本机扬声器出声，所以连着蓝牙也听不到）。" +
                        "选「系统默认」以外的设备，报警会把声音直接送到该设备（如蓝牙音响）。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline
            )
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                RadioButton(
                    selected = settings.alarmDeviceId == 0 && settings.alarmDeviceAddr.isEmpty(),
                    onClick = {
                        vm.set { s -> s.copy(alarmDeviceId = 0, alarmDeviceAddr = "", alarmDeviceName = "") }
                    }
                )
                Text("系统默认（当前路由）")
            }
            outputs.forEach { dev ->
                val sel = if (settings.alarmDeviceAddr.isNotEmpty()) {
                    dev.address == settings.alarmDeviceAddr
                } else {
                    settings.alarmDeviceId == dev.id && settings.alarmDeviceAddr.isEmpty()
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(
                        selected = sel,
                        onClick = {
                            vm.set { s ->
                                s.copy(
                                    alarmDeviceId = dev.id,
                                    alarmDeviceAddr = dev.address,
                                    alarmDeviceName = deviceLabel(dev)
                                )
                            }
                        }
                    )
                    Text(deviceLabel(dev))
                }
            }
            if (outputs.isEmpty()) {
                Text("未检测到可用输出设备", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline)
            }
            Spacer(Modifier.height(4.dp))
            Button(
                onClick = { routeRefresh++ },
                modifier = Modifier.fillMaxWidth()
            ) { Text("刷新设备列表${if (settings.alarmDeviceName.isNotEmpty()) "（已选：${settings.alarmDeviceName}）" else ""}") }

            Spacer(Modifier.height(8.dp))
            // —— 分时段策略（白天 / 晚间）——
            Spacer(Modifier.height(16.dp))
            Text("分时段策略（白天 / 晚间）", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                "开启后可分别设置白天、晚间的阈值与报警策略，App 会按时段自动切换；" +
                        "未覆盖到的时段按白天策略执行。关闭则全部沿用上方全局设置。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline
            )
            Spacer(Modifier.height(8.dp))
            SwitchRow("启用分时段策略", settings.scheduleEnabled) {
                vm.set { s -> s.copy(scheduleEnabled = it) }
            }

            if (settings.scheduleEnabled) {
                Spacer(Modifier.height(10.dp))
                PeriodCard(
                    title = "白天",
                    startMin = settings.dayStartMin,
                    endMin = settings.dayEndMin,
                    threshold = settings.dayThresholdDb,
                    sustain = settings.daySustainSec,
                    alarmEnabled = settings.dayAlarmEnabled,
                    alarmDur = settings.dayAlarmDurationSec,
                    alarmCool = settings.dayAlarmCooldownSec,
                    onStart = { vm.set { s -> s.copy(dayStartMin = it) } },
                    onEnd = { vm.set { s -> s.copy(dayEndMin = it) } },
                    onThreshold = { vm.set { s -> s.copy(dayThresholdDb = it) } },
                    onSustain = { vm.set { s -> s.copy(daySustainSec = it) } },
                    onAlarm = { vm.set { s -> s.copy(dayAlarmEnabled = it) } },
                    onAlarmDur = { vm.set { s -> s.copy(dayAlarmDurationSec = it) } },
                    onAlarmCool = { vm.set { s -> s.copy(dayAlarmCooldownSec = it) } },
                )
                Spacer(Modifier.height(10.dp))
                PeriodCard(
                    title = "晚间",
                    startMin = settings.nightStartMin,
                    endMin = settings.nightEndMin,
                    threshold = settings.nightThresholdDb,
                    sustain = settings.nightSustainSec,
                    alarmEnabled = settings.nightAlarmEnabled,
                    alarmDur = settings.nightAlarmDurationSec,
                    alarmCool = settings.nightAlarmCooldownSec,
                    onStart = { vm.set { s -> s.copy(nightStartMin = it) } },
                    onEnd = { vm.set { s -> s.copy(nightEndMin = it) } },
                    onThreshold = { vm.set { s -> s.copy(nightThresholdDb = it) } },
                    onSustain = { vm.set { s -> s.copy(nightSustainSec = it) } },
                    onAlarm = { vm.set { s -> s.copy(nightAlarmEnabled = it) } },
                    onAlarmDur = { vm.set { s -> s.copy(nightAlarmDurationSec = it) } },
                    onAlarmCool = { vm.set { s -> s.copy(nightAlarmCooldownSec = it) } },
                )
                // 覆盖校验提示（空档 / 重叠）
                val tips = Schedule.validate(settings)
                if (tips.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    tips.forEach { t ->
                        Text(
                            "⚠ $t",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error
                        )
                    }
                }
            }

            Spacer(Modifier.height(8.dp))
            SwitchRow("触发录音", settings.recEnabled) {
                vm.set { s -> s.copy(recEnabled = it) }
            }
            SliderRow("触发前保留", "%.0f s".format(settings.recPreSec),
                settings.recPreSec, 0f, 10f) { vm.set { s -> s.copy(recPreSec = it) } }
            SliderRow("触发后录制", "%.0f s".format(settings.recPostSec),
                settings.recPostSec, 0f, 10f) { vm.set { s -> s.copy(recPostSec = it) } }

            // —— 录音存储限制 ——
            Spacer(Modifier.height(16.dp))
            Text("录音存储限制", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                "超出上限时的处理方式：覆盖最旧 = 自动删最早的一条再存新的；停止存储 = 不再保存新录音。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline
            )
            Spacer(Modifier.height(4.dp))
            SliderRow(
                "最多保留条数",
                if (settings.recMaxCount <= 0) "不限制" else "${settings.recMaxCount} 条",
                settings.recMaxCount.toFloat(), 0f, 500f, steps = 49
            ) { v -> vm.set { s -> s.copy(recMaxCount = ((v + 5f) / 10f).toInt() * 10) } }
            SliderRow(
                "最大占用空间",
                if (settings.recMaxStorageMb <= 0) "不限制" else "${settings.recMaxStorageMb} MB",
                settings.recMaxStorageMb.toFloat(), 0f, 2000f, steps = 19
            ) { v -> vm.set { s -> s.copy(recMaxStorageMb = ((v + 50f) / 100f).toInt() * 100) } }
            Row {
                FilterChip(
                    selected = settings.recOverflowPolicy == RecordingStore.POLICY_OVERWRITE,
                    onClick = { vm.set { s -> s.copy(recOverflowPolicy = RecordingStore.POLICY_OVERWRITE) } },
                    label = { Text("覆盖最旧") },
                    colors = FilterChipDefaults.filterChipColors(
                        selectedContainerColor = MaterialTheme.colorScheme.surfaceVariant
                    )
                )
                Spacer(Modifier.width(8.dp))
                FilterChip(
                    selected = settings.recOverflowPolicy == RecordingStore.POLICY_STOP,
                    onClick = { vm.set { s -> s.copy(recOverflowPolicy = RecordingStore.POLICY_STOP) } },
                    label = { Text("停止存储") },
                    colors = FilterChipDefaults.filterChipColors(
                        selectedContainerColor = MaterialTheme.colorScheme.surfaceVariant
                    )
                )
            }
            Spacer(Modifier.height(4.dp))
            Text(
                "当前已有 $recCount 条录音，占用 ${RecordingStore.human(recBytes)}。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary
            )
            Spacer(Modifier.height(6.dp))
            Button(
                onClick = { confirmClear = true },
                enabled = recCount > 0,
                modifier = Modifier.fillMaxWidth()
            ) { Text("清除全部录音") }

            Spacer(Modifier.height(16.dp))
            Text("后台保活", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            val pm = remember { context.getSystemService(Context.POWER_SERVICE) as PowerManager }
            val ignoring = remember { pm.isIgnoringBatteryOptimizations(context.packageName) }
            Text(
                if (ignoring) "✅ 已在电池优化白名单，锁屏/后台更不易被冻结"
                else "未加白名单：锁屏后系统可能冻结监测，建议申请",
                style = MaterialTheme.typography.bodySmall,
                color = if (ignoring) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline
            )
            if (!ignoring) {
                Spacer(Modifier.height(6.dp))
                Button(
                    onClick = {
                        runCatching {
                            context.startActivity(
                                Intent(
                                    AndroidSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                    Uri.parse("package:${context.packageName}")
                                )
                            )
                        }.onFailure {
                            runCatching {
                                context.startActivity(
                                    Intent(AndroidSettings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                                )
                            }
                        }
                    },
                    modifier = Modifier.fillMaxWidth()
                ) { Text("申请电池优化白名单") }
            }
            Spacer(Modifier.height(4.dp))
            Text(
                "监测为前台服务（START_STICKY），被系统回收后会自动重启；上滑划掉应用后系统也会尝试拉起。" +
                        "配合白名单可长时间后台监测。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline
            )

            Spacer(Modifier.height(16.dp))
            Text(
                "录音保存在 App 私有目录：Android/data/com.volmon.app/files/recordings/。" +
                        "在「录音回放」里可试听、多选转发或一键清空。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline
            )
        }
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("清除全部录音？") },
            text = {
                Text("将删除全部 $recCount 条录音（${RecordingStore.human(recBytes)}），此操作不可恢复。")
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    val n = RecordingStore.clearAll(context)
                    recCount = RecordingStore.count(context)
                    recBytes = RecordingStore.totalBytes(context)
                    Toast.makeText(context, "已删除 $n 条录音", Toast.LENGTH_SHORT).show()
                }) { Text("全部删除") }
            },
            dismissButton = {
                TextButton(onClick = { confirmClear = false }) { Text("取消") }
            }
        )
    }
}

@Composable
private fun SliderRow(
    label: String,
    valueText: String,
    value: Float,
    min: Float,
    max: Float,
    steps: Int = 0,
    onValueChange: (Float) -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(label, Modifier.weight(1f))
            Text(valueText)
        }
        Slider(
            value = value,
            onValueChange = onValueChange,
            valueRange = min..max,
            steps = steps
        )
    }
}

/**
 * 数字输入框 + 显式「保存」按钮：输入过程只改本地状态（不会被重组冲掉），
 * 点「保存」或键盘「完成」才把值写入 DataStore，确保一定能存住。
 */
@Composable
private fun NumberFieldRow(
    label: String,
    value: Float,
    min: Float = 0f,
    max: Float = 60f,
    onSave: (Float) -> Unit,
) {
    var text by remember { mutableStateOf("%.2f".format(value)) }
    fun commit() {
        val v = text.toFloatOrNull()
        if (v != null && v >= min && v <= max) {
            onSave(v)
            text = "%.2f".format(v)
        }
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth()
    ) {
        OutlinedTextField(
            value = text,
            onValueChange = { text = it },
            label = { Text(label) },
            keyboardOptions = KeyboardOptions(
                keyboardType = KeyboardType.Decimal,
                imeAction = ImeAction.Done
            ),
            keyboardActions = KeyboardActions(onDone = { commit() }),
            singleLine = true,
            modifier = Modifier.weight(1f)
        )
        Spacer(Modifier.width(8.dp))
        Button(onClick = { commit() }) { Text("保存") }
    }
}

/**
 * 单个时段（白天 / 晚间）的配置卡片：起止时刻 + 5 个覆盖项。
 * 复用既有 SliderRow / NumberFieldRow / SwitchRow。
 */
@Composable
private fun PeriodCard(
    title: String,
    startMin: Int,
    endMin: Int,
    threshold: Float,
    sustain: Float,
    alarmEnabled: Boolean,
    alarmDur: Float,
    alarmCool: Float,
    onStart: (Int) -> Unit,
    onEnd: (Int) -> Unit,
    onThreshold: (Float) -> Unit,
    onSustain: (Float) -> Unit,
    onAlarm: (Boolean) -> Unit,
    onAlarmDur: (Float) -> Unit,
    onAlarmCool: (Float) -> Unit,
) {
    Surface(
        tonalElevation = 2.dp,
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.small,
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TimeFieldRow("开始", startMin, onStart, Modifier.weight(1f))
                TimeFieldRow("结束", endMin, onEnd, Modifier.weight(1f))
            }
            Spacer(Modifier.height(4.dp))
            NumberFieldRow("预警阈值（dB）", threshold, 40f, 110f) { onThreshold(it) }
            NumberFieldRow("持续触发（秒，最小精度 0.01）", sustain, 0f, 60f) { onSustain(it) }
            SwitchRow("超阈值播放报警声", alarmEnabled) { onAlarm(it) }
            SliderRow("报警声时长", "%.0f s".format(alarmDur), alarmDur, 1f, 30f) { onAlarmDur(it) }
            SliderRow("报警冷却", "%.0f s".format(alarmCool), alarmCool, 1f, 30f) { onAlarmCool(it) }
        }
    }
}

/**
 * 时刻输入：只读文本框显示 HH:MM，点击弹出 TimePicker 对话框，确认后回调分钟数。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TimeFieldRow(
    label: String,
    minutes: Int,
    onSave: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    var showDialog by remember { mutableStateOf(false) }
    val hh = minutes / 60
    val mm = minutes % 60
    OutlinedTextField(
        value = "%02d:%02d".format(hh, mm),
        onValueChange = {},
        readOnly = true,
        label = { Text(label) },
        modifier = modifier.clickable { showDialog = true },
        singleLine = true,
    )
    if (showDialog) {
        val state = rememberTimePickerState(initialHour = hh, initialMinute = mm)
        AlertDialog(
            onDismissRequest = { showDialog = false },
            title = { Text("$label 时刻") },
            text = { TimePicker(state = state) },
            confirmButton = {
                TextButton(onClick = {
                    onSave(state.hour * 60 + state.minute)
                    showDialog = false
                }) { Text("确定") }
            },
            dismissButton = {
                TextButton(onClick = { showDialog = false }) { Text("取消") }
            }
        )
    }
}

@Composable
private fun SwitchRow(label: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

/** 输出设备的中文类型名。 */
private fun deviceTypeName(t: Int): String = when (t) {
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "手机扬声器"
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "听筒"
    AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "有线耳机"
    AudioDeviceInfo.TYPE_WIRED_HEADSET -> "有线耳机/耳麦"
    AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "蓝牙音响(A2DP)"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "蓝牙(通话)"
    AudioDeviceInfo.TYPE_USB_HEADSET -> "USB 耳机"
    AudioDeviceInfo.TYPE_USB_DEVICE -> "USB 音频"
    AudioDeviceInfo.TYPE_HEARING_AID -> "助听器"
    AudioDeviceInfo.TYPE_DOCK -> "底座音响"
    AudioDeviceInfo.TYPE_LINE_ANALOG -> "线路输出"
    else -> "音频设备#$t"
}

/** 输出设备显示名：类型 + 产品名（产品名为空时仅类型）。 */
private fun deviceLabel(d: AudioDeviceInfo): String {
    val product = d.productName?.toString()?.takeIf { it.isNotBlank() }
    val type = deviceTypeName(d.type)
    return if (product != null) "$type · $product" else type
}
