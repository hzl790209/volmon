"""volmon 入口：子命令 serve / web / cli / calibrate / config / quiet / logs。

示例：
  python main.py serve                 # 启动后端服务（采集+广播+Web）
  python main.py web                   # 浏览器打开 Web 页面
  python main.py cli                   # 终端实时波形
  python main.py calibrate 65.0        # 用专业设备实测 65dB 校准
  python main.py quiet 60              # 60 分钟内超阈值也不发声（监测与记录照常）
  python main.py quiet --cancel        # 立即取消静音
  python main.py config set threshold_db 70
  python main.py logs --hours 24 --export data/export.csv
  python main.py logs --files                # 日志文件清单（按日切割 + 归档）
  python main.py logs --rotate [--force]     # 立刻做一次按日切割
"""
import argparse
import asyncio
import logging
import os
import sys
import time
import webbrowser

# 确保项目根目录在 sys.path，便于直接运行
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# **打包后必须先切到用户数据目录**，再导入配置/存储模块：
# .app 包内部是只读的，data/ 必须落在 ~/Library/Application Support/volmon-mac
from volmon import paths as _paths  # noqa: E402

_paths.setup_runtime()

# 仅顶层导入轻量模块；重型依赖（sounddevice/aiohttp/websockets）延迟到具体命令内导入，
# 以便未安装依赖时仍可执行 config / logs 等命令。
from volmon import storage, alerting, config as config_mod  # noqa: E402


def setup_logging(log_path, rotate_daily=True, keep_days_getter=None):
    """配置日志：控制台 + 按日切割的文件。

    rotate_daily=True 时，data/volmon.log 只装**今天**的日志，
    跨日自动归档为 data/volmon_YYYY-MM-DD.log，并清理超过保留天数的归档。
    keep_days_getter: 返回保留天数的回调（网页改了保留天数后下次切割即生效）。
    """
    from volmon import logrot

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if rotate_daily:
        file_handler = logrot.DailyRotatingFileHandler(
            log_path, encoding="utf-8", keep_days_getter=keep_days_getter
        )
    else:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):  # 避免重复添加（同一进程内多次调用）
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(logging.StreamHandler())
    return file_handler


def build_core(cfg, engine=None):
    if engine is None:
        from volmon import audio  # 延迟导入（需要 sounddevice）

        engine = audio.AudioEngine(device=cfg.get("device"))
    store = storage.Storage(
        cfg.get("db_path"),
        alerts_csv=cfg.get("alerts_csv_path"),
        csv_rotate_daily=bool(cfg.get("log_rotate_daily")),
        csv_keep_days=int(cfg.get("log_keep_days") or 0),
    )
    store.ensure_alerts_csv()  # 本地 CSV 不存在/表头过旧时用历史告警重建（并按日拆档）
    alm = alerting.AlertManager(
        store, threshold=cfg.get("threshold_db"), sustain_sec=cfg.get("sustain_sec")
    )
    return engine, store, alm


def cmd_serve(args, cfg):
    from volmon import server as server_mod  # 延迟导入（需要 aiohttp）
    from volmon import sim
    from volmon import alarm as alarm_mod

    engine = sim.SimEngine() if getattr(args, "fake", False) else None
    # 报警声播放器：仅真实模式发声；--fake 演示下不实际出声（逻辑照常）
    alarm_mgr = alarm_mod.AlarmManager(
        enabled=cfg.get("alarm_enabled"),
        duration_sec=cfg.get("alarm_duration_sec"),
        cooldown_sec=cfg.get("alarm_cooldown_sec"),
        output_device=cfg.get("alarm_output_device"),
    )
    # 播放器在**每次发声时**读取当前输出设备，网页改了立即生效，无需重启服务
    if not getattr(args, "fake", False):
        alarm_mgr.set_player(
            alarm_mod.make_sounddevice_player(device_getter=lambda: alarm_mgr.output_device)
        )
    eng, store, alm = build_core(cfg, engine)
    srv = server_mod.MonitorServer(eng, store, cfg, alm, alarm=alarm_mgr)

    async def start_background(app):
        eng.start()
        app["task"] = asyncio.create_task(srv.loop())

    async def cleanup(app):
        t = app.get("task")
        if t:
            t.cancel()
        eng.stop()

    srv.app.on_startup.append(start_background)
    srv.app.on_cleanup.append(cleanup)
    host = args.host or cfg.get("host")
    port = args.port or cfg.get("port")
    logging.getLogger("volmon").info(
        "服务启动: http://%s:%d  (Ctrl+C 停止)", host, port
    )
    try:
        from aiohttp import web

        web.run_app(srv.app, host=host, port=port, print=None)
    except KeyboardInterrupt:
        pass


def cmd_rec(args, cfg):
    """触发录音：查看状态 / 列出录音 / 手动清理。"""
    from volmon import recorder as recorder_mod

    rec = recorder_mod.Recorder(cfg)
    if getattr(args, "prune", False):
        removed = rec.prune()
        print(f"已清理 {len(removed)} 个录音文件")
        for r in removed[:20]:
            print("  - " + r)
        if len(removed) > 20:
            print(f"  … 另有 {len(removed) - 20} 个")
        print(f"剩余：{rec.stats()['count']} 个 / {rec.stats()['total_mb']} MB")
        return
    if getattr(args, "list", False):
        st = rec.stats()
        print(f"=== 录音文件（{st['count']} 个 / {st['total_mb']} MB）===")
        for f in reversed(st["recent"]):
            print(f"  {f['file']}　{f['size'] / 1024:.0f} KB　{f['mtime']}")
        print(f"\n播放：open {rec.base_dir()}/<日期>/<文件名>")
        return

    print("=== 触发录音 ===")
    print(f"开关：{'已开启' if cfg.get('rec_enabled') else '关闭（默认关闭，涉及录音请自行确认后开启）'}")
    trig = {"alert": "正式报警时", "over": "刚超过阈值时", "off": "不自动录"}.get(
        cfg.get("rec_trigger"), str(cfg.get("rec_trigger")))
    print(f"触发时机：{trig}")
    print(f"时长：触发前 {cfg.get('rec_pre_sec')} s + 触发后 {cfg.get('rec_post_sec')} s"
          f"（前置由常驻环形缓冲提供，不会漏掉开头）")
    print(f"目录：{cfg.get('rec_dir')}（按日分目录）")
    print(f"保留：{cfg.get('rec_keep_days')} 天 / 最多 {cfg.get('rec_max_files')} 个 / {cfg.get('rec_max_mb')} MB")
    st = rec.stats()
    print(f"当前：{st['count']} 个文件 / {st['total_mb']} MB")
    print("\n命令：")
    print("  python main.py rec --list    列出最近录音")
    print("  python main.py rec --prune   立即按保留策略清理")
    print("  python main.py config set rec_enabled true    # 开启（true/false）")


def cmd_mail(args, cfg):
    """每日报警邮件：查看配置 / 立即发送 / 预览。"""
    from volmon import mailer as mailer_mod

    if getattr(args, "send", False) or getattr(args, "preview", False):
        payload = {}
        if getattr(args, "day", None):
            payload["day"] = args.day
        if getattr(args, "to", None):
            payload["to"] = args.to
        if getattr(args, "preview", False):
            payload["dry_run"] = True
        j = _rest_json("/api/mail/send", payload, cfg)
        if j is None:
            # 服务未运行：本地直接发（读取同一份 data/config.json）
            if getattr(args, "to", None):
                cfg.set("mail_to", args.to)
            _, store, _ = build_core(cfg)
            res = mailer_mod.send_daily_report(
                cfg, store, day=getattr(args, "day", None),
                dry_run=bool(getattr(args, "preview", False)),
            )
            print(("预览：" if getattr(args, "preview", False) else "")
                  + ("发送成功 → " if res["ok"] else "失败 → ") + res["msg"])
            if res["ok"]:
                cfg.set("mail_last_result", f"{res.get('sent_at', '')} {res['msg']}")
            return
        if not j.get("ok"):
            print("失败：" + str(j.get("error") or j.get("msg")))
            return
        print(("预览：" if getattr(args, "preview", False) else "") + j["msg"])
        return

    # 默认：显示配置与状态
    info = mailer_mod.public_config(cfg)
    print("=== 每日报警邮件 ===")
    print(f"开关：{'已开启' if info['mail_enabled'] else '关闭'}"
          f"　每天 {info['mail_send_time']} 发送"
          f"　报表范围：{'昨天全天' if int(info['mail_day_offset'] or 0) else '今天 00:00 至今'}")
    print(f"SMTP：{info['mail_host']}:{info['mail_port']}"
          f"　{'SSL' if info['mail_ssl'] else ('STARTTLS' if info['mail_starttls'] else '明文')}")
    print(f"发件：{info['mail_user'] or '（未设置）'}　授权码：{'已设置' if info['mail_password_set'] else '（未设置）'}")
    print(f"收件：{info['mail_to'] or '（未设置，将发给发件人自己）'}")
    print(f"附件 CSV：{'是' if info['mail_attach_csv'] else '否'}"
          f"　无告警时跳过：{'是' if info['mail_skip_if_empty'] else '否'}")
    print(f"最近一次：{info['mail_last_result'] or '（尚未发送过）'}")
    print("\n命令：")
    print("  python main.py mail --preview          预览报表内容（不发送）")
    print("  python main.py mail --send              立即发送昨天日报")
    print("  python main.py mail --send --day 2026-09-26   补发指定日期")
    print("  python main.py config set mail_to you@example.com")


def cmd_app(args, cfg):
    """macOS .app 入口：启动服务并**自动打开浏览器**（双击图标即用，无需终端）。"""
    import threading

    import webbrowser

    host = args.host or cfg.get("host")
    port = args.port or cfg.get("port")
    url = f"http://127.0.0.1:{port}/"

    def _open_browser():
        # 等服务监听起来再打开，避免浏览器先报"无法连接"
        # 首启动可能较慢（加载音频库），最多等 30 秒
        import time

        for _ in range(60):
            time.sleep(0.5)
            try:
                import urllib.request

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                    break
            except Exception:
                continue
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open_browser, daemon=True).start()
    logging.getLogger("volmon").info("桌面版启动，稍后自动打开 %s", url)
    cmd_serve(args, cfg)


def cmd_web(args, cfg):
    url = f"http://{cfg.get('host') if cfg.get('host') not in ('0.0.0.0',) else '127.0.0.1'}:{cfg.get('port')}/"
    print(f"打开 Web 页面: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


def cmd_cli(args, cfg):
    from volmon import cli as cli_mod  # 延迟导入（需要 websockets/rich）

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or cfg.get("port")
    asyncio.run(cli_mod.run_cli(host=host, port=port))


def cmd_calibrate(args, cfg):
    from volmon import calibrate as calibrate_mod  # 延迟导入（需要 aiohttp）

    port = args.port or cfg.get("port")
    calibrate_mod.run_calibrate(args.real_db, args.host, port)


def cmd_config(args, cfg):
    if args.action == "get":
        if args.key:
            print(f"{args.key} = {cfg.get(args.key)}")
        else:
            for k, v in cfg.get_all().items():
                print(f"{k} = {v}")
        return
    # set
    if not args.key:
        print("用法: python main.py config set <key> <value>")
        return
    value = args.value
    if value is None:
        # 从标准输入或交互读取
        value = input(f"请输入 {args.key} 的值: ").strip()
    # 尝试数字
    try:
        value = float(value) if "." in value or "e" in value.lower() else int(value)
    except Exception:
        pass
    # 优先通过后端 REST 设置（若运行中）
    ok = _rest_set(cfg, args.key, value)
    if not ok:
        cfg.set(args.key, value)
        print(f"已本地保存: {args.key} = {cfg.get(args.key)}")
    else:
        print(f"已通过后端更新: {args.key} = {cfg.get(args.key)}")


def _rest_set(cfg, key, value):
    import urllib.request

    host = cfg.get("host")
    if host in ("0.0.0.0",):
        host = "127.0.0.1"
    url = f"http://{host}:{cfg.get('port')}/api/config"
    try:
        req = urllib.request.Request(
            url,
            data=json_dumps({key: value}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _fmt_dur(sec):
    sec = max(0, int(round(float(sec))))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}小时{m:02d}分" if h else f"{m:02d}:{s:02d}"


def _rest_json(path, payload=None, cfg=None):
    """调用后端 REST；服务未运行时返回 None。"""
    import json
    import urllib.request

    host = cfg.get("host") if cfg else "127.0.0.1"
    if host in ("0.0.0.0",):
        host = "127.0.0.1"
    url = f"http://{host}:{cfg.get('port')}{path}" if cfg else path
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url,
                data=json_dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def cmd_quiet(args, cfg):
    """报警静音时段：人离开期间超阈值也不发声，但监测与超量记录照常。

    服务在运行时走 REST 即时生效（无需重启）；否则落本地配置，下次启动生效。
    """
    if args.cancel:
        j = _rest_json("/api/quiet", {"cancel": True}, cfg)
        if j is None:
            cfg.set("quiet_until_ms", 0.0)
            print("已取消报警静音（本地配置；服务运行时会即时生效）")
        else:
            print("已取消报警静音，超阈值将恢复发声")
        return

    if args.minutes is None:
        j = _rest_json("/api/quiet", None, cfg)
        if j is None:
            until = float(cfg.get("quiet_until_ms") or 0.0)
            left = max(0.0, (until - time.time() * 1000) / 1000.0) if until else 0.0
            j = {
                "quiet_active": left > 0,
                "quiet_remaining_sec": left,
                "until_text": time.strftime("%H:%M:%S", time.localtime(until / 1000)) if left else "",
            }
        if j.get("quiet_active"):
            print(f"🔕 报警静音中：剩余 {_fmt_dur(j['quiet_remaining_sec'])}（到 {j.get('until_text', '')}）")
        else:
            print("当前未静音：超阈值会正常发声")
        return

    j = _rest_json("/api/quiet", {"minutes": args.minutes}, cfg)
    if j is None:
        until = time.time() * 1000 + args.minutes * 60000.0
        cfg.set("quiet_until_ms", until)
        print(
            f"已本地设置报警静音 {args.minutes:g} 分钟（到 "
            f"{time.strftime('%H:%M:%S', time.localtime(until / 1000))}）；服务运行时会即时生效"
        )
    else:
        print(
            f"🔕 报警静音已生效：{args.minutes:g} 分钟，到 {j.get('until_text', '')}；"
            f"期间监测与超量记录照常，只是不发声"
        )


def cmd_schedule(args, cfg):
    """分时段策略（白天 / 晚间）：查看状态或设置某一项。

    服务运行时走 REST 即时生效；否则落本地配置，下次启动生效。
    """
    from volmon import schedule as sched_mod

    if args.key and args.key not in sched_mod.SCHEDULE_KEYS:
        print(f"未知的时段配置项：{args.key}\n可选：{' / '.join(sched_mod.SCHEDULE_KEYS)}")
        return

    if args.key:
        value = args.value
        if value is None:
            print(f"{args.key} = {cfg.get(args.key)}")
            return
        try:
            value = float(value) if ("." in value or "e" in value.lower()) else int(value)
        except ValueError:
            pass  # 形如 "07:00" 的时间串保持字符串
        j = _rest_json("/api/schedule", {args.key: value}, cfg)
        if j is None:
            cfg.set(args.key, value)
            print(f"已本地保存（服务运行时会即时生效）：{args.key} = {cfg.get(args.key)}")
        else:
            print(f"已生效：{args.key} = {j['applied'].get(args.key)}")
        return

    j = _rest_json("/api/schedule", None, cfg)
    local = False
    if j is None:
        j = sched_mod.status(cfg)
        j["config"] = {k: cfg.get(k) for k in sched_mod.SCHEDULE_KEYS}
        local = True
    c = j.get("config", {})
    print("=== 分时段策略 ===")
    print(f"启用状态：{'已启用' if j['enabled'] else '未启用（沿用全局阈值与报警设置）'}"
          + ("　[服务未运行，显示本地配置]" if local else ""))
    print(f"白天：{c.get('day_start')} ~ {c.get('day_end')}"
          f"　阈值 {c.get('day_threshold_db')} dB / 持续 {c.get('day_sustain_sec')}s"
          f" / 报警 {'开' if c.get('day_alarm_enabled') else '关'}"
          f" ({c.get('day_alarm_duration_sec')}s + 冷却 {c.get('day_alarm_cooldown_sec')}s)")
    print(f"晚间：{c.get('night_start')} ~ {c.get('night_end')}"
          f"　阈值 {c.get('night_threshold_db')} dB / 持续 {c.get('night_sustain_sec')}s"
          f" / 报警 {'开' if c.get('night_alarm_enabled') else '关'}"
          f" ({c.get('night_alarm_duration_sec')}s + 冷却 {c.get('night_alarm_cooldown_sec')}s)")
    print(f"当前时段：{j['period_label']}　生效阈值 {j['threshold_db']} dB"
          f" / 持续 {j['sustain_sec']}s / 报警 {'开' if j['alarm_enabled'] else '关'}")
    print(f"距切换到另一时段：约 {_fmt_dur(j['next_switch_min'] * 60)}")
    for w in j.get("warnings", []):
        print(f"⚠ {w}")


def cmd_policy(args, cfg):
    """打印当前**生效**的一整套策略明细（含该策略来自全局还是分时段）。"""
    j = _rest_json("/api/policy", None, cfg)
    if j is None:
        print("服务未运行，无法读取实时生效策略。请先启动：python main.py serve")
        return
    dev = j.get("input_device") or {}
    print("=== 当前生效策略 ===")
    print(f"策略来源：{j['source']}"
          + ("（分时段已启用）" if j["schedule_enabled"] else "（分时段未启用）"))
    print(f"时段：{j['period_label']}"
          f"　白天 {j['windows']['day']['start']}~{j['windows']['day']['end']}"
          f"　晚间 {j['windows']['night']['start']}~{j['windows']['night']['end']}"
          f"　距切换约 {_fmt_dur(j['next_switch_min'] * 60)}")
    print(f"阈值：{j['threshold_db']} dB　持续：{j['sustain_sec']} s")
    print(f"声音报警：{'开启' if j['alarm_enabled'] else '关闭'}"
          f"　时长 {j['alarm_duration_sec']} s　冷却 {j['alarm_cooldown_sec']} s"
          f"　输出 {(j['alarm_output_device'] or '系统默认输出')}")
    print(f"报警静音：{'生效中，剩余 ' + _fmt_dur(j['quiet_remaining_sec']) if j['quiet_active'] else '未启用'}")
    print(f"采集设备：{dev.get('name') or '未知'}（索引 {dev.get('index')}，配置 {j['device_configured']}）")
    print(f"校准：{'已校准 ' + str(j['calib_offset_db']) + ' dB' if j['calibrated'] else '未校准（估算）'}")
    print(f"日志条数：采样 {j['counts'].get('samples')} 条 / 超量告警 {j['counts'].get('alerts')} 条")
    for w in j.get("warnings", []):
        print(f"⚠ {w}")


def cmd_clear(args, cfg):
    """清空日志：支持一键全清与按时间段清空。"""
    import datetime as _dt

    def parse_time(s):
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(_dt.datetime.strptime(s.strip(), fmt).timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError(f"无法解析时间：{s}（格式例：2026-09-27 14:00）")

    payload = {"scope": args.scope}
    if args.all:
        payload["all"] = True
    elif args.hours:
        payload["hours"] = args.hours
    elif args.start or args.end:
        if args.start:
            payload["start_ts"] = parse_time(args.start)
        if args.end:
            payload["end_ts"] = parse_time(args.end)
    else:
        print("请指定范围：--all（全部） / --hours N（近 N 小时） / --start ... --end ...")
        return

    if not args.yes:
        desc = "全部" if args.all else (f"近 {args.hours} 小时" if args.hours else f"{args.start or '最早'} ~ {args.end or '现在'}")
        if input(f"确定清空{desc}的日志（范围：{args.scope}）？该操作不可恢复 [y/N]: ").strip().lower() != "y":
            print("已取消")
            return

    j = _rest_json("/api/logs/clear", payload, cfg)
    if j is None:
        print("服务未运行，无法通过接口清空。请先启动服务（清空需与运行中的服务保持一致）。")
        return
    if not j.get("ok"):
        print("清空失败：" + str(j.get("error")))
        return
    d = j.get("deleted", {})
    print(f"已清空：采样 {d.get('samples', 0)} 条 / 超量告警 {d.get('alerts', 0)} 条")
    print(f"剩余：{j.get('remaining', {})}")
    print(f"本地 {cfg.get('alerts_csv_path')}（及受影响日期的归档 CSV）已同步重写")


def print_log_files(cfg):
    """打印按日切割后的日志文件清单（当前文件 + 归档）。"""
    from volmon import logrot

    paths = [
        cfg.get("log_path"),
        cfg.get("alerts_csv_path"),
        os.path.join(ROOT, "data", "restart.log"),
    ]
    print("=== 日志文件（按日切割）===")
    print(f"切割开关：{'开启' if cfg.get('log_rotate_daily') else '关闭'}"
          f"　归档保留 {cfg.get('log_keep_days')} 天（0 = 不删）")
    for item in logrot.log_files(paths):
        cur = item["current"]
        print(f"\n[{item['base']}]")
        if cur:
            print(f"  今天：{cur['name']}　{cur['size'] / 1024:.1f} KB　更新于 {cur['mtime']}")
        else:
            print("  今天：（暂无）")
        if not item["archives"]:
            print("  归档：（暂无）")
        for a in item["archives"]:
            print(f"  归档：{a['name']}　{a['size'] / 1024:.1f} KB　{a['mtime']}")
    print(f"\n采样库（不切割，单库）：{cfg.get('db_path')}"
          f"　用 python main.py clear 清理")


def cmd_logs_rotate(args, cfg):
    """立刻做一次按日切割（默认只在真的跨日时才动，--force 强制切）。"""
    from volmon import logrot

    keep = int(cfg.get("log_keep_days") or 0)
    j = _rest_json("/api/logs/rotate", {"force": bool(getattr(args, "force", False))}, cfg)
    if j is None:
        # 服务未运行：只做文件级切割（运行日志的 handler 在服务进程里，交给服务启动时处理）
        done = []
        for p in (cfg.get("log_path"), cfg.get("alerts_csv_path"),
                  os.path.join(ROOT, "data", "restart.log")):
            if not p:
                continue
            a = (logrot.archive_file(p, logrot.day_str(), keep) if getattr(args, "force", False)
                 else logrot.rotate_file_if_stale(p, keep))
            if a:
                done.append(a)
            logrot.prune_archives(p, keep)
        print(f"服务未运行，已做文件级切割：归档 {len(done)} 个" + ("" if not done else " -> " + ", ".join(os.path.basename(x) for x in done)))
        print_log_files(cfg)
        return
    if not j.get("ok"):
        print("切割失败：" + str(j.get("error")))
        return
    print(f"已归档 {len(j.get('archived', []))} 个，清理过期 {len(j.get('deleted', []))} 个")
    for p in j.get("archived", []):
        print("  + " + os.path.basename(str(p)))
    for p in j.get("deleted", []):
        print("  - " + os.path.basename(str(p)))


def cmd_logs(args, cfg):
    if getattr(args, "files", False):
        print_log_files(cfg)
        return
    if getattr(args, "rotate", False):
        cmd_logs_rotate(args, cfg)
        return
    _, store, _ = build_core(cfg)
    since = int((__import__("time").time() - args.hours * 3600) * 1000)
    alerts = store.get_alerts(since)
    print(f"=== 近 {args.hours}h 超量告警事件 ({len(alerts)} 条) ===")
    for a in alerts:
        print(
            f"  峰值 {a['peak_db']:.1f}dB  阈值 {a['threshold_db']:.1f}  持续 {a['duration_s']}s"
        )
    if args.export:
        n = store.export_csv(args.export, since)
        print(f"已导出采样 {n} 条 -> {args.export}")
    if args.export_alerts:
        n = store.export_alerts_csv(args.export_alerts, since)
        print(f"已导出超量告警 {n} 条 -> {args.export_alerts}")
    print(f"提示：超量告警同时实时追加写入 {cfg.get('alerts_csv_path')}（Excel 可直接打开）")


def json_dumps(obj):
    import json

    return json.dumps(obj, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(prog="volmon", description="音量/噪音监控软件")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动后端服务（采集+Web+广播）")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--fake", action="store_true", help="无麦克风时用模拟引擎演示/测试")

    sub.add_parser("web", help="浏览器打开 Web 页面")
    # 供打包后的 .app 调用（桌面版入口：起服务 + 自动开浏览器）
    p_app = sub.add_parser("app", help="桌面版入口：启动服务并自动打开浏览器")
    p_app.add_argument("--host", default=None)
    p_app.add_argument("--port", type=int, default=None)
    p_app.add_argument("--fake", action="store_true", help="无麦克风时用模拟引擎演示/测试")

    p_cli = sub.add_parser("cli", help="终端实时波形客户端")
    p_cli.add_argument("--host", default=None)
    p_cli.add_argument("--port", type=int, default=None)

    p_cal = sub.add_parser("calibrate", help="用专业设备实测值校准")
    p_cal.add_argument("real_db", type=float, help="专业设备测得的真实 dB")
    p_cal.add_argument("--host", default="127.0.0.1")
    p_cal.add_argument("--port", type=int, default=None)

    p_cfg = sub.add_parser("config", help="查看/设置配置")
    p_cfg.add_argument("action", choices=["get", "set"])
    p_cfg.add_argument("key", nargs="?")
    p_cfg.add_argument("value", nargs="?")

    p_quiet = sub.add_parser(
        "quiet",
        help="报警静音时段：设定期间超阈值也不发声（监测与超量记录照常）",
    )
    p_quiet.add_argument("minutes", type=float, nargs="?", default=None, help="静音分钟数（不给则查询状态）")
    p_quiet.add_argument("--cancel", action="store_true", help="取消静音，立即恢复发声")

    p_sched = sub.add_parser(
        "schedule",
        help="分时段策略（白天/晚间）：查看状态或设置某一项",
    )
    p_sched.add_argument("key", nargs="?", default=None,
                         help="配置项名，如 day_threshold_db / night_start（省略则查看状态）")
    p_sched.add_argument("value", nargs="?", default=None)

    p_policy = sub.add_parser("policy", help="显示当前生效策略明细")

    p_clear = sub.add_parser("clear", help="清空日志（一键全清 / 按时间段清空）")
    p_clear.add_argument("--scope", default="all", choices=["all", "samples", "alerts"],
                         help="清空范围：采样+告警 / 仅采样 / 仅告警")
    p_clear.add_argument("--all", action="store_true", help="清空全部（不限时间）")
    p_clear.add_argument("--hours", type=float, default=None, help="只清近 N 小时")
    p_clear.add_argument("--start", default=None, help="开始时间，如 '2026-09-27 14:00'")
    p_clear.add_argument("--end", default=None, help="结束时间，如 '2026-09-27 18:00'")
    p_clear.add_argument("--yes", action="store_true", help="跳过确认提示")

    p_rec = sub.add_parser("rec", help="触发录音：把报警时的真实声音存成 WAV")
    p_rec.add_argument("--list", action="store_true", help="列出最近录音文件")
    p_rec.add_argument("--prune", action="store_true", help="立即按保留策略清理")

    p_mail = sub.add_parser("mail", help="每日报警日志邮件推送（查看配置/预览/发送）")
    p_mail.add_argument("--send", action="store_true", help="立即发送一次日报")
    p_mail.add_argument("--preview", action="store_true", help="只生成报表不发送（预览）")
    p_mail.add_argument("--day", default=None, help="指定日期 YYYY-MM-DD（补发用）")
    p_mail.add_argument("--to", default=None, help="临时指定收件人")

    p_logs = sub.add_parser("logs", help="查看超量告警日志/导出采样/日志文件清单")
    p_logs.add_argument("--hours", type=float, default=24)
    p_logs.add_argument("--export", default=None, help="导出采样为 CSV")
    p_logs.add_argument("--export-alerts", default=None, help="导出超量告警为 CSV")
    p_logs.add_argument("--files", action="store_true", help="列出按日切割后的日志文件清单")
    p_logs.add_argument("--rotate", action="store_true", help="立刻做一次按日切割与过期清理")
    p_logs.add_argument("--force", action="store_true", help="配合 --rotate：未跨日也强制归档")

    args = parser.parse_args()

    cfg = config_mod.Config()
    host = getattr(args, "host", None)
    if host:
        cfg.set("host", host)
    if getattr(args, "port", None):
        cfg.set("port", args.port)

    setup_logging(
        cfg.get("log_path"),
        rotate_daily=bool(cfg.get("log_rotate_daily")),
        keep_days_getter=lambda: int(cfg.get("log_keep_days") or 0),
    )

    if args.cmd == "serve":
        cmd_serve(args, cfg)
    elif args.cmd == "app":
        cmd_app(args, cfg)
    elif args.cmd == "web":
        cmd_web(args, cfg)
    elif args.cmd == "cli":
        cmd_cli(args, cfg)
    elif args.cmd == "calibrate":
        cmd_calibrate(args, cfg)
    elif args.cmd == "config":
        cmd_config(args, cfg)
    elif args.cmd == "quiet":
        cmd_quiet(args, cfg)
    elif args.cmd == "schedule":
        cmd_schedule(args, cfg)
    elif args.cmd == "policy":
        cmd_policy(args, cfg)
    elif args.cmd == "clear":
        cmd_clear(args, cfg)
    elif args.cmd == "logs":
        cmd_logs(args, cfg)
    elif args.cmd == "mail":
        cmd_mail(args, cfg)
    elif args.cmd == "rec":
        cmd_rec(args, cfg)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
