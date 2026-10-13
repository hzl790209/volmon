"""命令行实时客户端：连接后端 WebSocket，在终端渲染**带坐标轴的实时音量图表**。

呈现方式对齐 Web 页面：
  - 纵轴：音量 dB（自适应刻度，含阈值）
  - 横轴：时间（左=起点、中=中间、右=现在）
  - 红色虚线行：预警阈值
  - 顶部状态条：报警/超量/正常 + 校准状态
字符全部使用 ASCII，保证在任意终端下对齐不错位。
"""
import asyncio
import json
import logging
import math
import shutil
import time

import websockets
from rich.console import Console
from rich.live import Live
from rich.text import Text

logger = logging.getLogger("volmon.cli")

# 纵轴标签区宽度（如 "   90 |"）
LAB_W = 7


def _fmt_dur(sec):
    """把秒数格式化为 MM:SS（超过 1 小时为 H小时MM分）。"""
    sec = max(0, int(round(float(sec))))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}小时{m:02d}分" if h else f"{m:02d}:{s:02d}"


def _range_for(vals, thr):
    """自适应 Y 轴范围：含阈值，取整到 10，最小跨度 30dB（与 Web 端一致）。"""
    mn, mx = min(vals), max(vals)
    mn = min(mn, thr)
    mx = max(mx, thr)
    lo = math.floor((mn - 5) / 10.0) * 10
    hi = math.ceil((mx + 5) / 10.0) * 10
    if hi - lo < 30:
        c = (hi + lo) / 2.0
        lo = math.floor((c - 15) / 5.0) * 5
        hi = math.ceil((c + 15) / 5.0) * 5
    return float(lo), float(hi)


def build_chart(hist, threshold, width, height, reserve=12):
    """把 [(时间戳ms, dB), ...] 渲染成字符图表。

    reserve: 右侧为阈值标注预留的字符宽度，避免终端换行错位。
    返回 (lines, plot_w, lo, hi, data)，lines 为 [(标签, 内容, 是否阈值行), ...]。
    """
    vals = [d for _, d in hist]
    lo, hi = _range_for(vals, threshold)
    plot_w = max(10, min(width - LAB_W - reserve, 200))
    rows = max(5, min(height, 24))
    data = hist[-plot_w:] if len(hist) > plot_w else hist

    canvas = [[" "] * plot_w for _ in range(rows)]

    def y_row(v):
        v = min(max(v, lo), hi)
        return min(max(int(round((hi - v) / (hi - lo) * (rows - 1))), 0), rows - 1)

    thr_row = y_row(threshold)

    # 描点
    n = len(data)
    for i, (_, v) in enumerate(data):
        c = int(round(i * (plot_w - 1) / max(n - 1, 1)))
        canvas[y_row(v)][c] = "*"
    # 竖直连线，让波形连续
    for c in range(plot_w):
        rs = [r for r in range(rows) if canvas[r][c] == "*"]
        if len(rs) >= 2:
            for rr in range(min(rs), max(rs)):
                if canvas[rr][c] == " ":
                    canvas[rr][c] = "|"

    lines = []
    for r in range(rows):
        val = hi - (hi - lo) * r / (rows - 1)
        label = f"{val:>5.0f} |"
        if r == thr_row:
            content = "".join("-" if canvas[r][c] == " " else canvas[r][c] for c in range(plot_w))
        else:
            content = "".join(canvas[r][c] for c in range(plot_w))
        lines.append((label, content, r == thr_row))
    return lines, plot_w, lo, hi, data


def _xlabels(data, plot_w):
    """横轴时间刻度：左=起点、中=中间、右=现在。"""
    lbl = [" "] * plot_w

    def put(s, pos):
        pos = max(0, min(plot_w - len(s), pos))
        for i, ch in enumerate(s):
            lbl[pos + i] = ch

    def f(ms):
        return time.strftime("%H:%M:%S", time.localtime(ms / 1000))

    t0, t1 = data[0][0], data[-1][0]
    put(f(t0), 0)
    put(f((t0 + t1) / 2), plot_w // 2 - 4)
    put(f(t1) + " 现在", plot_w - 14)
    return "".join(lbl)


def render(db, raw_db, threshold, over, alert, calibrated, history, peak,
           alarm_phase="idle", mic_silent=False, width=100, height=14,
           version="", quiet_sec=0.0, period_label="", schedule_applied=False):
    t = Text()
    status = "未校准(估算)" if not calibrated else "已校准"
    t.append("音量监控终端  ", style="bold cyan")
    if alarm_phase == "playing":
        t.append(" 报警播放中(静默) ", style="bold white on magenta")
    elif alarm_phase == "cooldown":
        t.append(" 冷却中(静默) ", style="bold magenta")
    elif alert:
        t.append(" 超量告警! ", style="bold white on red")
    elif over:
        t.append(" 超阈值 ", style="bold red")
    else:
        t.append(" 正常 ", style="bold green")
    t.append(f"[{status}]")
    if version:
        # 已经是完整产品版本（如 "volmon-mac 1.0"）就直接显示，否则补个 v 前缀
        label = version if " " in version else f"v{version}"
        t.append(f"  {label}", style="dim")
    if period_label:
        tag = f"{period_label}策略" if schedule_applied else f"{period_label}（全局）"
        t.append(f"  [{tag}]", style="bold blue")
    if quiet_sec > 0:
        t.append(f"  [静音中 剩 {_fmt_dur(quiet_sec)}]", style="bold yellow")
    t.append("\n")

    t.append(f"当前: {db:6.1f} dB", style="bold")
    t.append(f"  (原始 {raw_db:6.1f} dBFS)", style="dim")
    t.append(f"  阈值: {threshold:6.1f}  峰值: {peak:6.1f}\n")

    if not history:
        t.append("等待数据…\n", style="dim")
        return t

    lines, plot_w, lo, hi, data = build_chart(history, threshold, width, height)
    for label, content, is_thr in lines:
        t.append(label, style="dim")
        if is_thr:
            # 行是离散的，行标签可能与阈值有偏差，此处标注真实阈值
            t.append(content.rstrip(), style="bold red")
            t.append(f"  <=阈值{threshold:.1f}\n", style="bold red")
        else:
            t.append(content.rstrip() + "\n", style="bold cyan")

    t.append(" " * LAB_W + "-" * plot_w + "\n", style="dim")
    t.append(" " * LAB_W + _xlabels(data, plot_w).rstrip() + "\n", style="dim")
    t.append(
        f"纵轴=音量 dB ({lo:.0f}~{hi:.0f})   横轴=时间   红色行=预警阈值 {threshold:.1f} dB\n",
        style="dim",
    )

    if quiet_sec > 0:
        t.append(
            f"静音时段：剩余 {_fmt_dur(quiet_sec)}，期间超阈值不发声（监测与超量记录照常）\n",
            style="bold yellow",
        )
    if mic_silent:
        t.append("! 麦克风持续静音(-100 dBFS)：疑似未授权麦克风/输入被静音/设备选错\n", style="bold red")
    return t


async def run_cli(host="127.0.0.1", port=8000):
    url = f"ws://{host}:{port}/ws"
    console = Console()
    history = []  # [(ts_ms, db), ...]
    try:
        async with websockets.connect(url, ping_interval=10) as ws:
            console.print(f"[green]已连接 {url}[/green]")
            with Live(console=console, refresh_per_second=15, screen=True) as live:
                async for message in ws:
                    data = json.loads(message)
                    db = data.get("db", 0.0)
                    raw = data.get("raw_db", 0.0)
                    thr = data.get("threshold", 70.0)
                    over = data.get("over", False)
                    alert = data.get("alert", False)
                    peak = data.get("peak", db)
                    cal = data.get("calibrated", False)
                    phase = data.get("alarm_phase", "idle")
                    mic_silent = data.get("mic_silent", False)
                    ver = data.get("version_display") or data.get("version", "")
                    quiet_sec = float(data.get("quiet_remaining_sec", 0.0) or 0.0)
                    period_label = data.get("period_label", "") or ""
                    sched_on = bool(data.get("schedule_applied", False))

                    history.append((data.get("t", time.time() * 1000), db))
                    if len(history) > 600:
                        history = history[-600:]

                    size = shutil.get_terminal_size(fallback=(100, 30))
                    live.update(
                        render(
                            db, raw, thr, over, alert, cal, history, peak,
                            phase, mic_silent,
                            width=size.columns,
                            height=max(8, size.lines - 12),
                            version=ver,
                            quiet_sec=quiet_sec,
                            period_label=period_label,
                            schedule_applied=sched_on,
                        )
                    )
    except ConnectionRefusedError:
        console.print(
            f"[red]无法连接后端 {url}[/red]\n请先运行: [bold]python main.py serve[/bold]"
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]已退出 CLI[/yellow]")
