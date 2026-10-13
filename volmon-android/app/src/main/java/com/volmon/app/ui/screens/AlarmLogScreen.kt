package com.volmon.app.ui.screens

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.DeleteSweep
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.navigation.NavController
import com.volmon.app.data.AlarmEntry
import com.volmon.app.data.AlarmStore
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

private val dayFmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)
private val timeFmt = SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)

/**
 * 报警记录：汇总统计（今日/累计/峰值/均值）+ 近 7 天报警趋势柱状图 + 明细列表，
 * 支持清空（带确认）。数据来自 [AlarmStore]。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AlarmLogScreen(nav: NavController) {
    val context = LocalContext.current
    var entries by remember { mutableStateOf<List<AlarmEntry>>(emptyList()) }
    var showClear by remember { mutableStateOf(false) }

    fun reload() { entries = AlarmStore.readAll(context) }
    LaunchedEffect(Unit) { reload() }

    val sorted = entries.sortedByDescending { it.timeMs }
    val todayKey = dayFmt.format(Date())
    val todayCount = entries.count { dayFmt.format(Date(it.timeMs)) == todayKey }
    val maxDb = entries.maxOfOrNull { it.peakDb } ?: 0f
    val avgDb = if (entries.isEmpty()) 0f else entries.map { it.peakDb }.average().toFloat()

    // 近 7 天每天的报警次数（含今天，旧→新）
    val days: List<Pair<String, Int>> = (6 downTo 0).map { back ->
        val c = Calendar.getInstance()
        c.add(Calendar.DAY_OF_YEAR, -back)
        val key = dayFmt.format(c.time)
        key to entries.count { dayFmt.format(Date(it.timeMs)) == key }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("报警记录") },
                navigationIcon = {
                    IconButton(onClick = { nav.popBackStack() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
                actions = {
                    IconButton(onClick = { if (entries.isNotEmpty()) showClear = true }) {
                        Icon(Icons.Filled.DeleteSweep, contentDescription = "清空")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            if (entries.isEmpty()) {
                Spacer(Modifier.weight(1f))
                Text("暂无报警记录", fontSize = 18.sp)
                Spacer(Modifier.height(8.dp))
                Text(
                    "超过阈值并持续达标后会记录在这里。",
                    fontSize = 13.sp, color = MaterialTheme.colorScheme.outline
                )
                Spacer(Modifier.weight(1f))
            } else {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                    StatCell("今日", todayCount.toString())
                    StatCell("累计", entries.size.toString())
                    StatCell("峰值", "%.0f dB".format(maxDb))
                    StatCell("均值", "%.1f dB".format(avgDb))
                }
                Spacer(Modifier.height(14.dp))
                Text(
                    "近 7 天报警趋势",
                    fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                AlarmTrendChart(days, Modifier.fillMaxWidth().height(130.dp))
                Spacer(Modifier.height(10.dp))
                Text(
                    "报警明细（最近在前）",
                    fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                LazyColumn(Modifier.fillMaxWidth().weight(1f)) {
                    items(sorted.take(300), key = { it.timeMs }) { e ->
                        Row(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
                            Text(
                                timeFmt.format(Date(e.timeMs)),
                                modifier = Modifier.weight(1f), fontSize = 13.sp
                            )
                            Text(
                                "%.1f dB".format(e.peakDb),
                                fontSize = 13.sp,
                                color = MaterialTheme.colorScheme.primary,
                                fontWeight = FontWeight.Medium
                            )
                        }
                        HorizontalDivider(color = MaterialTheme.colorScheme.surfaceVariant)
                    }
                }
            }
        }
    }

    if (showClear) {
        AlertDialog(
            onDismissRequest = { showClear = false },
            title = { Text("清空报警记录") },
            text = { Text("将删除全部 ${entries.size} 条报警记录，且不可恢复。") },
            confirmButton = {
                TextButton(onClick = {
                    AlarmStore.clear(context)
                    showClear = false
                    reload()
                }) { Text("清空") }
            },
            dismissButton = { TextButton(onClick = { showClear = false }) { Text("取消") } }
        )
    }
}

@Composable
private fun StatCell(label: String, value: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(
            value, fontSize = 18.sp, fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.primary
        )
        Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
    }
}

/** 近 7 天报警次数柱状图（纯 Canvas，无第三方图表库）。 */
@Composable
private fun AlarmTrendChart(days: List<Pair<String, Int>>, modifier: Modifier = Modifier) {
    val barColor = MaterialTheme.colorScheme.primary
    val gridColor = MaterialTheme.colorScheme.outline.copy(alpha = 0.30f)
    val labelColor = MaterialTheme.colorScheme.outline
    val maxCount = (days.maxOfOrNull { it.second } ?: 0).coerceAtLeast(1)

    Canvas(modifier) {
        val w = size.width
        val h = size.height
        val labelH = 18.dp.toPx()
        val chartH = (h - labelH).coerceAtLeast(1f)

        for (i in 0..2) {
            val y = chartH * i / 2f
            drawLine(gridColor, Offset(0f, y), Offset(w, y), strokeWidth = 1f)
        }

        val paint = android.graphics.Paint().apply {
            isAntiAlias = true
            textSize = 10.dp.toPx()
            textAlign = android.graphics.Paint.Align.CENTER
            color = labelColor.toArgb()
        }

        val slot = w / days.size
        days.forEachIndexed { i, (key, count) ->
            val barW = slot * 0.5f
            val x = slot * i + (slot - barW) / 2f
            val barH = if (count == 0) 0f else chartH * count / maxCount
            if (barH > 0f) {
                drawRect(barColor, Offset(x, chartH - barH), Size(barW, barH))
            }
            drawContext.canvas.nativeCanvas.drawText(
                key.substring(5),           // "MM-dd"
                x + barW / 2f,
                h - 4.dp.toPx(),
                paint
            )
        }
    }
}
