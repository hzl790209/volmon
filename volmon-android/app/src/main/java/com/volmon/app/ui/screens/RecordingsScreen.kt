package com.volmon.app.ui.screens

import android.content.Intent
import android.media.MediaPlayer
import android.net.Uri
import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.material.icons.filled.Checklist
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.DeleteSweep
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.SelectAll
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
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
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import androidx.navigation.NavController
import com.volmon.app.data.RecordingStore
import java.io.File

/**
 * 录音回放：
 *  - 单条播放 / 停止、单条删除；
 *  - 「选择」进入多选模式 → 全选 → 一键转发（ACTION_SEND / SEND_MULTIPLE）；
 *  - 「清空」一键清除所有录音（二次确认）。
 * 顶部还会显示当前条数与总占用，方便对照设置页的数量/空间限制。
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun RecordingsScreen(nav: NavController) {
    val context = LocalContext.current
    var files by remember { mutableStateOf<List<File>>(emptyList()) }
    var playingPath by remember { mutableStateOf<String?>(null) }
    var selectMode by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf<Set<String>>(emptySet()) }
    var confirmClear by remember { mutableStateOf(false) }
    var confirmDeleteSelected by remember { mutableStateOf(false) }
    val player = remember { MediaPlayer() }

    fun refresh() {
        val list = RecordingStore.list(context)
        files = list
        // 清理已不存在的选择项
        val alive = list.map { it.absolutePath }.toSet()
        selected = selected.intersect(alive)
        if (list.isEmpty()) selectMode = false
    }

    LaunchedEffect(Unit) { refresh() }
    DisposableEffect(Unit) {
        onDispose { runCatching { player.release() } }
    }

    fun stopPlayback() {
        runCatching { if (player.isPlaying) player.stop() }
        runCatching { player.reset() }
        playingPath = null
    }

    fun togglePlay(f: File) {
        runCatching {
            if (playingPath == f.absolutePath) {
                stopPlayback()
            } else {
                player.reset()
                player.setDataSource(f.absolutePath)
                player.prepare()
                player.start()
                playingPath = f.absolutePath
            }
        }.onFailure { playingPath = null }
    }

    fun delete(f: File) {
        if (playingPath == f.absolutePath) stopPlayback()
        runCatching { f.delete() }
        selected = selected - f.absolutePath
        refresh()
    }

    fun toggleSelect(f: File) {
        selected = if (f.absolutePath in selected) selected - f.absolutePath
        else selected + f.absolutePath
    }

    fun shareFiles(targets: List<File>) {
        if (targets.isEmpty()) return
        val uris = ArrayList<Uri>()
        for (f in targets) {
            runCatching {
                uris.add(
                    FileProvider.getUriForFile(context, RecordingStore.authority(context), f)
                )
            }
        }
        if (uris.isEmpty()) {
            Toast.makeText(context, "无法生成分享链接", Toast.LENGTH_SHORT).show()
            return
        }
        val intent = if (uris.size == 1) {
            Intent(Intent.ACTION_SEND).apply {
                type = "audio/x-wav"
                putExtra(Intent.EXTRA_STREAM, uris[0])
                putExtra(Intent.EXTRA_SUBJECT, targets[0].name)
            }
        } else {
            Intent(Intent.ACTION_SEND_MULTIPLE).apply {
                type = "audio/x-wav"
                putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
                putExtra(Intent.EXTRA_SUBJECT, "volmon 录音 ${uris.size} 条")
            }
        }
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        runCatching {
            context.startActivity(Intent.createChooser(intent, "转发录音"))
        }.onFailure {
            Toast.makeText(context, "没有可用的分享目标", Toast.LENGTH_SHORT).show()
        }
    }

    val totalBytes = remember(files) { files.sumOf { it.length() } }

    Scaffold(
        topBar = {
            if (selectMode) {
                TopAppBar(
                    title = { Text("已选 ${selected.size} / ${files.size} 项") },
                    navigationIcon = {
                        IconButton(onClick = {
                            selectMode = false
                            selected = emptySet()
                        }) { Icon(Icons.Filled.Close, contentDescription = "退出选择") }
                    },
                    actions = {
                        val allSelected = files.isNotEmpty() && selected.size == files.size
                        IconButton(onClick = {
                            selected = if (allSelected) emptySet()
                            else files.map { it.absolutePath }.toSet()
                        }) {
                            Icon(
                                Icons.Filled.SelectAll,
                                contentDescription = if (allSelected) "取消全选" else "全选"
                            )
                        }
                        IconButton(
                            enabled = selected.isNotEmpty(),
                            onClick = { shareFiles(files.filter { it.absolutePath in selected }) }
                        ) {
                            Icon(Icons.Filled.Share, contentDescription = "转发")
                        }
                        IconButton(
                            enabled = selected.isNotEmpty(),
                            onClick = { confirmDeleteSelected = true }
                        ) {
                            Icon(
                                Icons.Filled.Delete,
                                contentDescription = "删除选中",
                                tint = if (selected.isNotEmpty()) MaterialTheme.colorScheme.error
                                else MaterialTheme.colorScheme.outline
                            )
                        }
                    }
                )
            } else {
                TopAppBar(
                    title = { Text("录音回放") },
                    navigationIcon = {
                        IconButton(onClick = { nav.popBackStack() }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                        }
                    },
                    actions = {
                        if (files.isNotEmpty()) {
                            IconButton(onClick = {
                                selected = emptySet()
                                selectMode = true
                            }) { Icon(Icons.Filled.Checklist, contentDescription = "选择") }
                            IconButton(onClick = { confirmClear = true }) {
                                Icon(Icons.Filled.DeleteSweep, contentDescription = "清除全部")
                            }
                        }
                    }
                )
            }
        }
    ) { padding ->
        if (files.isEmpty()) {
            Column(
                modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center
            ) {
                Text("暂无录音", fontSize = 18.sp)
                Spacer(Modifier.height(8.dp))
                Text(
                    "在设置里开启「触发录音」后，报警时会自动保存 WAV 到这里。",
                    fontSize = 13.sp, color = MaterialTheme.colorScheme.outline
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(bottom = 16.dp)
            ) {
                item {
                    Row(
                        Modifier.fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            "共 ${files.size} 条 · ${RecordingStore.human(totalBytes)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.outline,
                            modifier = Modifier.weight(1f)
                        )
                        Text(
                            "长按可多选",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.outline
                        )
                    }
                    HorizontalDivider(color = MaterialTheme.colorScheme.surfaceVariant)
                }
                items(files, key = { it.absolutePath }) { f ->
                    val isPlaying = playingPath == f.absolutePath
                    val checked = f.absolutePath in selected
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .combinedClickable(
                                onClick = {
                                    if (selectMode) toggleSelect(f) else togglePlay(f)
                                },
                                onLongClick = {
                                    // 长按进入多选并选中当前项
                                    if (!selectMode) {
                                        selectMode = true
                                        selected = setOf(f.absolutePath)
                                    } else {
                                        toggleSelect(f)
                                    }
                                }
                            ),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        if (selectMode) {
                            Checkbox(checked = checked, onCheckedChange = { toggleSelect(f) })
                        } else {
                            IconButton(onClick = { togglePlay(f) }) {
                                Icon(
                                    if (isPlaying) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                                    contentDescription = if (isPlaying) "停止" else "播放",
                                    tint = if (isPlaying) MaterialTheme.colorScheme.primary
                                    else MaterialTheme.colorScheme.onSurface
                                )
                            }
                        }
                        Column(Modifier.weight(1f)) {
                            Text(
                                f.name.removeSuffix(RecordingStore.SUFFIX).replace('_', ' '),
                                fontWeight = if (checked) FontWeight.Bold else FontWeight.Normal
                            )
                            Text(
                                RecordingStore.human(f.length()),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.outline
                            )
                        }
                        if (!selectMode) {
                            IconButton(onClick = { delete(f) }) {
                                Icon(
                                    Icons.Filled.Delete, contentDescription = "删除",
                                    tint = MaterialTheme.colorScheme.outline
                                )
                            }
                        }
                    }
                    HorizontalDivider(color = MaterialTheme.colorScheme.surfaceVariant)
                }
            }
        }
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("清除全部录音？") },
            text = {
                Text("将删除当前全部 ${files.size} 条录音（${RecordingStore.human(totalBytes)}），此操作不可恢复。")
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    stopPlayback()
                    val n = RecordingStore.clearAll(context)
                    selected = emptySet()
                    selectMode = false
                    refresh()
                    Toast.makeText(context, "已删除 $n 条录音", Toast.LENGTH_SHORT).show()
                }) { Text("全部删除") }
            },
            dismissButton = {
                TextButton(onClick = { confirmClear = false }) { Text("取消") }
            }
        )
    }

    if (confirmDeleteSelected) {
        val targets = files.filter { it.absolutePath in selected }
        val delBytes = targets.sumOf { it.length() }
        AlertDialog(
            onDismissRequest = { confirmDeleteSelected = false },
            title = { Text("删除选中的 ${targets.size} 条录音？") },
            text = {
                Text("将删除选中的 ${targets.size} 条录音（${RecordingStore.human(delBytes)}），此操作不可恢复。")
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmDeleteSelected = false
                    // 正在播放的若被删，先停
                    if (playingPath != null && playingPath in selected) stopPlayback()
                    var n = 0
                    for (t in targets) if (runCatching { t.delete() }.getOrDefault(false)) n++
                    selected = emptySet()
                    selectMode = false
                    refresh()
                    Toast.makeText(context, "已删除 $n 条录音", Toast.LENGTH_SHORT).show()
                }) { Text("删除选中") }
            },
            dismissButton = {
                TextButton(onClick = { confirmDeleteSelected = false }) { Text("取消") }
            }
        )
    }
}
