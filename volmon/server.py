"""后端服务：aiohttp 提供 WebSocket 实时广播、REST 配置/采样/告警/设备接口、托管 Web 页面。"""
import asyncio
import json
import logging
import os
import time

from aiohttp import web, WSMsgType
from volmon import __display__, __product__, __version__
from volmon import alarm as alarm_mod
from volmon import config as config_mod
from volmon import logrot as logrot_mod
from volmon import mailer as mailer_mod
from volmon import paths as paths_mod
from volmon import recorder as recorder_mod
from volmon import schedule as schedule_mod
from volmon.audio import DBFS_FLOOR, device_group_key, enriched_device_list

logger = logging.getLogger("volmon.server")

# 数据/日志根目录：源码运行=项目根；打包后=~/Library/Application Support/volmon-mac
ROOT = paths_mod.data_root()

# 可由网页自定义的存储路径（网页「存储路径」面板）。
#   kind="dir"  -> 该项本身是目录（校验/创建它自身）
#   kind="file" -> 该项是文件（校验/创建它的**父目录**）
#   need_restart=True  -> 改动已写入配置，但要等下次启动才真正落到新位置：
#       日志 handler 与数据库/CSV 句柄在服务启动时绑定，运行中换不了；
#   need_restart=False -> 即时生效（录音目录由录音器每次写盘时读取 config）。
PATH_SPECS = (
    ("rec_dir",         "dir",  False, "录音存储目录"),
    ("log_path",        "file", True,  "运行日志文件"),
    ("db_path",         "file", True,  "采样数据库"),
    ("alerts_csv_path", "file", True,  "超量告警 CSV"),
)

# 允许网页/WS 修改的配置项（其余为内部项，避免误改）
CONFIG_KEYS = {
    "threshold_db",
    "sustain_sec",
    "sample_interval_ms",
    "log_interval_ms",
    "calib_offset_db",
    "device_calib",
    "meter_smooth_sec",
    "device",
    "ref_spl_offset_db",
    "alarm_enabled",
    "alarm_duration_sec",
    "alarm_cooldown_sec",
    "alarm_output_device",
    "quiet_until_ms",
    "log_rotate_daily",
    "log_keep_days",
    "rec_enabled",
    "rec_trigger",
    "rec_pre_sec",
    "rec_post_sec",
    "rec_keep_days",
    "rec_max_files",
    "rec_max_mb",
    "rec_cut_on_alarm",
    "rec_gain_mode",
    "rec_gain_db",
    "show_all_devices",      # 设备下拉「显示全部设备」开关（浏览器换请求参数即可临时覆盖）
    # 自定义存储路径（日志/录音/数据库/CSV）；网页「存储路径」面板专用
    *{k for k, _kind, _rs, _label in PATH_SPECS},
    *mailer_mod.MAIL_KEYS,
    *schedule_mod.SCHEDULE_KEYS,
}


class MonitorServer:
    def __init__(self, engine, storage, config, alerting, alarm=None):
        self.engine = engine
        self.storage = storage
        self.config = config
        self.alerting = alerting
        self.alarm = alarm if alarm is not None else alarm_mod.AlarmManager()
        self.clients = set()
        self._last_log_ts = 0
        self._cur_input_device = None   # 已生效的输入设备（用于热切换检测）
        self._input_dev_info = None     # 采集流**实际**在用的设备信息
        self._alerting_params = None    # 已同步到 AlertManager 的 (阈值, 持续时长)
        self._policy_key = None         # 已同步的"生效策略"快照（用于变化检测）
        self._last_mail_check = 0.0     # 上次检查"是否该发每日邮件"的时刻
        self._mail_fail_day = ""        # 每日邮件连续失败计数（同一天最多重试 3 次）
        self._mail_fail_count = 0
        self.recorder = recorder_mod.Recorder(config)
        self._prev_alert = False        # 上一轮是否处于告警态（用于**边沿触发**录音）
        self._prev_over = False         # 上一轮是否超阈值
        self._rec_attached = False
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/ws", self.handle_ws)
        self.app.router.add_get("/api/health", self.health)
        self.app.router.add_get("/api/calib", self.get_calib)
        self.app.router.add_post("/api/calib", self.post_calib)
        self.app.router.add_get("/api/config", self.get_config)
        self.app.router.add_post("/api/config", self.post_config)
        self.app.router.add_get("/api/paths", self.get_paths)
        self.app.router.add_post("/api/paths", self.post_paths)
        self.app.router.add_get("/api/samples", self.get_samples)
        self.app.router.add_get("/api/alerts", self.get_alerts)
        self.app.router.add_get("/api/alerts/count", self.get_alerts_count)
        self.app.router.add_get("/api/trend/export", self.get_trend_export)
        self.app.router.add_get("/api/devices", self.get_devices)
        self.app.router.add_post("/api/devices/rescan", self.post_devices_rescan)
        self.app.router.add_get("/api/output-devices", self.get_output_devices)
        self.app.router.add_post("/api/device-alias", self.post_device_alias)
        self.app.router.add_get("/api/raw_avg", self.get_raw_avg)
        self.app.router.add_get("/api/alarm/test", self.alarm_test)
        self.app.router.add_post("/api/alarm/test", self.alarm_test)
        self.app.router.add_post("/api/config/reset", self.post_config_reset)
        self.app.router.add_post("/api/service/restart", self.post_service_restart)
        self.app.router.add_post("/api/service/stop", self.post_service_stop)
        self.app.router.add_get("/api/status", self.get_status)
        self.app.router.add_get("/api/schedule", self.get_schedule)
        self.app.router.add_post("/api/schedule", self.post_schedule)
        self.app.router.add_get("/api/policy", self.get_policy)
        self.app.router.add_post("/api/logs/clear", self.post_logs_clear)
        self.app.router.add_get("/api/logs/files", self.get_logs_files)
        self.app.router.add_post("/api/logs/rotate", self.post_logs_rotate)
        self.app.router.add_get("/api/mail", self.get_mail)
        self.app.router.add_post("/api/mail/send", self.post_mail_send)
        self.app.router.add_get("/api/recordings", self.get_recordings)
        self.app.router.add_post("/api/recordings/prune", self.post_recordings_prune)
        self.app.router.add_post("/api/recordings/clear", self.post_recordings_clear)
        self.app.router.add_get("/api/audio/{tail:.*}", self.get_audio)
        self.app.router.add_get("/api/quiet", self.get_quiet)
        self.app.router.add_post("/api/quiet", self.post_quiet)

    # ---------- 页面与接口 ----------
    async def handle_index(self, request):
        # 打包后 web/ 在 sys._MEIPASS 里，不能用 __file__ 推导
        p = paths_mod.resource_path("web", "index.html")
        try:
            with open(p, "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html")
        except FileNotFoundError:
            return web.Response(text="index.html 未找到", status=404)

    async def health(self, request):
        return web.json_response({
            "ok": True,
            "version": __version__,
            "product": __product__,
            "version_display": __display__,
        })

    async def get_config(self, request):
        return web.json_response(self.config.get_all())

    async def post_config(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        allowed = CONFIG_KEYS
        applied = {}
        for k, v in data.items():
            if k in allowed:
                self.config.set(k, v)
                applied[k] = self.config.get(k)
        # 同步报警参数
        self.alerting.update_params(
            self.config.get("threshold_db"), self.config.get("sustain_sec")
        )
        # 关键：网页改了「报警声输出设备」后立即同步到运行中的告警管理器，
        # 不必等下一轮 loop 才生效（loop 也会再同步一次，这里先即时生效更稳妥）。
        # 否则在某些机器上用户改完设备、立刻点「试听」会仍从旧设备出声，表现为"修改不生效"。
        try:
            self.alarm.sync_from_config(self.config.get_all())
        except Exception as e:  # 同步失败不应让保存接口报错
            logger.warning("同步告警运行参数失败（下次循环会重试）: %s", e)
        self._sync_log_retention()
        logger.info("配置已更新: %s", applied)
        return web.json_response({"ok": True, "applied": applied, "config": self.config.get_all()})

    def _sync_log_retention(self):
        """网页改了「按日切割 / 保留天数」后，立即同步给告警 CSV 落盘逻辑。

        运行日志的保留天数由 handler 在下次跨日切割时读取（不必重启）。
        """
        try:
            self.storage.set_csv_retention(
                rotate_daily=self.config.get("log_rotate_daily"),
                keep_days=self.config.get("log_keep_days"),
            )
        except Exception:
            pass

    # ---------- 自定义存储路径（网页「存储路径」面板） ----------
    def _path_state(self):
        """汇总每项存储路径：当前值 / 解析后的绝对路径 / 目录是否存在且可写 / 是否需重启。"""
        out = {}
        for key, kind, need_rs, label in PATH_SPECS:
            raw = self.config.get(key) or ""
            resolved = paths_mod.resolve_user_path(raw) or ""
            target = ""
            exists = writable = False
            if resolved:
                target = resolved if kind == "dir" else os.path.dirname(resolved)
                exists = os.path.isdir(target)
                writable = exists and os.access(target, os.W_OK)
            out[key] = {
                "label": label,
                "kind": kind,
                "value": raw,
                "resolved": resolved,
                "target_dir": target,
                "exists": exists,
                "writable": writable,
                "need_restart": need_rs,
                "default": config_mod.DEFAULTS.get(key, ""),
                # 默认值也解析成绝对路径：面板一律以绝对路径展示/回填，
                # 用户看到的"默认路径"就是真实落盘位置，而不是相对写法。
                "default_resolved": paths_mod.resolve_user_path(
                    config_mod.DEFAULTS.get(key, "")) or "",
            }
        return out

    async def get_paths(self, request):
        """GET /api/paths：当前存储路径设置（含解析后的绝对路径与可写性）。"""
        return web.json_response({"ok": True, "root": ROOT, "paths": self._path_state()})

    async def post_paths(self, request):
        """POST /api/paths：保存自定义存储路径。

        请求体只含要改的键，值为用户填的路径（绝对或相对均可）。
        每个路径都**先建目录并验证可写**再写入配置，避免服务被指到一个写不进的位置
        （那会让日志/录音静默失效，正是本功能最容易踩的坑）。
        返回 applied / errors / need_restart。
        """
        try:
            body = await request.json() or {}
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        applied, errors, need_restart = {}, {}, False
        for key, kind, need_rs, label in PATH_SPECS:
            if key not in body:
                continue
            raw = str(body.get(key) or "").strip()
            if raw == (self.config.get(key) or ""):
                continue                       # 与当前值相同，无需处理
            try:
                if not raw:
                    errors[key] = "不能为空（要恢复默认请点「恢复默认路径」）"
                    continue
                resolved = paths_mod.resolve_user_path(raw)
                target = resolved if kind == "dir" else os.path.dirname(resolved)
                os.makedirs(target, exist_ok=True)
                if not os.path.isdir(target):
                    errors[key] = f"不是有效目录：{target}"
                    continue
                if not os.access(target, os.W_OK):
                    errors[key] = f"目录不可写（权限不足）：{target}"
                    continue
                # 存用户原始写法（保留相对/绝对语义），展示时才解析成绝对路径
                self.config.set(key, raw)
                applied[key] = {"value": raw, "resolved": resolved, "label": label}
                logger.info("存储路径已更新（%s）：%s -> %s",
                            "需重启生效" if need_rs else "立即生效", key, resolved)
                if need_rs:
                    need_restart = True
            except Exception as e:
                errors[key] = str(e)

        state = self._path_state()
        if errors:
            return web.json_response({"ok": False, "errors": errors,
                                      "applied": applied, "paths": state}, status=400)
        return web.json_response({"ok": True, "applied": applied,
                                  "need_restart": need_restart, "paths": state})

    async def get_samples(self, request):
        hours = float(request.query.get("hours", 24))
        since = int((time.time() - hours * 3600) * 1000)
        return web.json_response(self.storage.get_samples(since))

    async def get_alerts(self, request):
        """超量告警列表（支持分页：limit / offset，默认返回全部）。"""
        hours = float(request.query.get("hours", 24))
        since = int((time.time() - hours * 3600) * 1000)
        rows = self.storage.get_alerts(since)
        limit = request.query.get("limit")
        if limit:
            offset = int(request.query.get("offset", 0))
            rows = rows[offset : offset + int(limit)]
        return web.json_response(rows)

    async def get_alerts_count(self, request):
        """超量告警总数（供前端分页计算页数）。"""
        hours = float(request.query.get("hours", 24))
        since = int((time.time() - hours * 3600) * 1000)
        return web.json_response({"total": len(self.storage.get_alerts(since))})

    async def get_trend_export(self, request):
        """GET /api/trend/export?hours=24：把「历史趋势」当前区间导出为 CSV。

        文件内含：导出信息、统计汇总（含预警次数）、字符趋势图、预警明细、
        原始数据（每行带迷你趋势图列）。带 Content-Disposition，点击即下载。
        """
        hours = float(request.query.get("hours", 24))
        now_ms = int(time.time() * 1000)
        since = int(now_ms - hours * 3600 * 1000)
        samples = self.storage.get_samples(since)
        alerts = self.storage.get_alerts_range(since, now_ms)
        threshold = self.config.get("threshold_db")
        label = "近 7 天" if hours >= 168 else ("近 %g 小时" % hours)
        csv_text = build_trend_csv(samples, alerts, threshold, label, since, now_ms)
        fname = "volmon_trend_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
        # 注意：aiohttp 不允许把 charset 写进 content_type，必须用单独的 charset 参数
        return web.Response(
            text=csv_text,
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": 'attachment; filename="%s"' % fname},
        )

    async def post_config_reset(self, request):
        """一键恢复默认设置：把 data/config.json 还原为 DEFAULTS（含阈值/校准/报警/设备）。"""
        cfg = self.config.reset()
        self.alerting.update_params(
            self.config.get("threshold_db"), self.config.get("sustain_sec")
        )
        logger.info("配置已恢复默认")
        return web.json_response({"ok": True, "config": cfg})

    async def post_service_restart(self, request):
        """重启服务：统一走任务计划（与看门狗同一权威），避免进程内 fork 产生重复实例。

        新任务实例会先清掉旧的 serve / watchdog 孤儿进程再各拉起一个，当前进程稍后退出，
        因此重启后系统里仍然只有 1 个服务 + 1 个看门狗，杜绝此前的 2+2 重复问题。
        """
        try:
            from volmon import watchdog as wd
            ok, msg = wd.restart_service()
        except Exception as e:
            return web.json_response({"ok": False, "msg": f"重启失败: {e}"}, status=500)
        logger.info("收到重启指令：%s", msg)
        if ok:
            # 新实例会先清理旧服务再拉起；当前进程稍后退出，绝不重复拉起。
            asyncio.get_running_loop().create_task(self._exit_after(1.5))
            return web.json_response({"ok": True, "msg": "服务正在重启，约 2 秒后请刷新页面"})
        return web.json_response(
            {"ok": False, "msg": "已下发重启指令，但任务触发失败：%s（看门狗会重试）" % msg},
            status=500)

    async def post_service_stop(self, request):
        """停止服务。

        桌面版（.app）没有窗口也没有菜单栏，双击后看不出进程在哪——
        这个接口给网页上的「停止服务」按钮用，让用户可以正常退出程序。
        置「停止保护」标记，看门狗不会在用户刚停下时立刻把它重新拉起。
        """
        try:
            from volmon import watchdog as wd
            wd.set_stop_guard()
        except Exception:
            pass
        logger.info("收到停止指令：服务即将退出（已置停止保护，看门狗不再自动拉起）")
        asyncio.get_running_loop().create_task(self._exit_after(0.8))
        return web.json_response({"ok": True, "msg": "服务已停止（桌面版即退出程序）"})

    async def _exit_after(self, delay):
        import asyncio as _a

        await _a.sleep(delay)
        os._exit(0)

    async def get_status(self, request):
        """排障用：返回**实际在用的**输入设备与报警输出设备。"""
        try:
            info = self.engine.active_device_info(self.config.get("device_aliases"))
        except Exception:
            info = None
        return web.json_response({
            "ok": True,
            "version": __version__,
            "product": __product__,
            "version_display": __display__,
            "input_device": info,
            "configured_input": self.config.get("device") or "default",
            "alarm_output_device": self.config.get("alarm_output_device") or "",
        })

    async def get_schedule(self, request):
        """分时段策略状态：当前时段、生效的阈值/报警设置、距下次切换、配置校验提示。"""
        return web.json_response({
            "ok": True,
            "config": {k: self.config.get(k) for k in schedule_mod.SCHEDULE_KEYS},
            **schedule_mod.status(self.config),
        })

    async def post_schedule(self, request):
        """设置分时段策略（只接受时段相关配置键）。改动立即生效，无需重启服务。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        applied = {}
        for k, v in (body or {}).items():
            if k in schedule_mod.SCHEDULE_KEYS:
                self.config.set(k, v)
                applied[k] = self.config.get(k)
        logger.info("分时段策略已更新: %s", applied)
        # 分时段策略改了报警开关/时长后即时同步到运行中的告警管理器
        try:
            self.alarm.sync_from_config(self.config.get_all())
        except Exception as e:
            logger.warning("同步告警运行参数失败（下次循环会重试）: %s", e)
        return web.json_response({"ok": True, "applied": applied,
                                  **schedule_mod.status(self.config)})

    # ---------- 当前生效策略明细 ----------
    def policy_snapshot(self):
        """把"此刻真正生效的一整套策略"汇总成一目了然的明细。"""
        st = schedule_mod.status(self.config)
        try:
            dev = self.engine.active_device_info(self.config.get("device_aliases"))
        except Exception:
            dev = None
        q = self._quiet_state()
        cal_off, cal_key, cal_dev, cal_src = self.calib_state()
        try:
            counts = self.storage.counts()
        except Exception:
            counts = {"samples": None, "alerts": None}
        return {
            "version": __version__,
            "product": __product__,
            "version_display": __display__,
            "source": f"分时段 · {st['period_label']}" if st["applied"] else "全局设置",
            "schedule_enabled": st["enabled"],
            "schedule_applied": st["applied"],
            "period": st["period"],
            "period_label": st["period_label"],
            "next_switch_min": st["next_switch_min"],
            "windows": st["windows"],
            "threshold_db": st["threshold_db"],
            "sustain_sec": st["sustain_sec"],
            "alarm_enabled": st["alarm_enabled"],
            "alarm_duration_sec": st["alarm_duration_sec"],
            "alarm_cooldown_sec": st["alarm_cooldown_sec"],
            "alarm_output_device": self.config.get("alarm_output_device") or "",
            "alarm_phase": self.alarm.phase(time.time() * 1000),
            "quiet_active": q["quiet_active"],
            "quiet_remaining_sec": q["quiet_remaining_sec"],
            "quiet_until_text": q["until_text"],
            "device_configured": self.config.get("device") or "default",
            "input_device": dev,
            "input_hostapi": (dev or {}).get("hostapi") or "",
            "input_fallback": bool((dev or {}).get("fallback")),
            "input_fallback_reason": (dev or {}).get("fallback_reason") or "",
            "calibrated": cal_src in ("path", "device"),
            "calib_source": cal_src,   # path=本机本通路 / device=本机其它通路 / global=旧全局值 / estimate=未校准
            "calib_exact": cal_src == "path",
            "calib_offset_db": round(cal_off, 2),         # 当前**实际生效**的偏移
            "calib_device": cal_dev,
            "calib_device_key": cal_key,
            "device_calib": dict(self.config.get("device_calib") or {}),
            "global_calib_offset_db": float(self.config.get("calib_offset_db") or 0),
            "ref_spl_offset_db": float(self.config.get("ref_spl_offset_db") or 0),
            "meter_smooth_sec": float(self.config.get("meter_smooth_sec") or 0),
            "warnings": st["warnings"],
            "counts": counts,
        }

    async def get_policy(self, request):
        """GET /api/policy：当前生效策略明细（页面「当前生效策略」面板与排障用）。"""
        return web.json_response({"ok": True, **self.policy_snapshot()})

    async def post_logs_clear(self, request):
        """POST /api/logs/clear：清空日志。

        请求体：
          {"scope": "all"|"samples"|"alerts"}       默认 all
          {"all": true}                             清空全部（不限时间）
          {"hours": 24}                             只清近 24 小时
          {"start_ts": ms, "end_ts": ms}            按时间段清空（闭区间）
        清空告警后会同步重写本地 data/alerts.csv，避免"库里没了、CSV 里还在"。
        """
        try:
            body = await request.json() or {}
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        scope = body.get("scope") or "all"
        if scope not in ("all", "samples", "alerts"):
            return web.json_response({"ok": False, "error": f"未知范围: {scope}"}, status=400)

        now_ms = time.time() * 1000
        if body.get("all"):
            since = until = None
        elif body.get("hours") is not None:
            try:
                hours = float(body["hours"])
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "hours 必须是数字"}, status=400)
            if hours <= 0:
                return web.json_response({"ok": False, "error": "hours 必须为正数"}, status=400)
            since, until = now_ms - hours * 3600 * 1000, now_ms
        else:
            since = body.get("start_ts")
            until = body.get("end_ts")
            if since is not None and until is not None and float(since) > float(until):
                return web.json_response({"ok": False, "error": "开始时间不能晚于结束时间"}, status=400)

        try:
            deleted = self.storage.clear(
                scope=scope,
                since_ts=None if since is None else int(float(since)),
                until_ts=None if until is None else int(float(until)),
            )
        except Exception as e:
            logger.warning("清空日志失败: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        try:
            counts = self.storage.counts()
        except Exception:
            counts = {}
        logger.info("清空日志：范围=%s 时间窗=%s~%s 删除 %s", scope, since, until, deleted)
        return web.json_response({"ok": True, "deleted": deleted, "remaining": counts})

    # ---------- 日志按日切割 ----------
    def _log_paths(self):
        return [
            self.config.get("log_path"),
            self.config.get("alerts_csv_path"),
            os.path.join(ROOT, "data", "restart.log"),
        ]

    def _log_handler(self):
        """找到当前进程里的按日切割 handler（没有则返回 None）。"""
        for h in logging.getLogger().handlers:
            if isinstance(h, logrot_mod.DailyRotatingFileHandler):
                return h
        return None

    async def get_logs_files(self, request):
        """GET /api/logs/files：按日切割后的日志文件清单。

        返回每类日志的「当前文件（今天）」与「历史归档（按天）」及大小。
        """
        return web.json_response({
            "ok": True,
            "rotate_daily": bool(self.config.get("log_rotate_daily")),
            "keep_days": int(self.config.get("log_keep_days") or 0),
            "db_path": self.config.get("db_path"),
            "files": logrot_mod.log_files(self._log_paths()),
        })

    async def post_logs_rotate(self, request):
        """POST /api/logs/rotate：立刻做一次按日切割与过期清理。

        请求体：{"force": true} 强制把当前文件归档（默认只在跨日时才切）。
        正常无需手动调用——跨天时服务会自动切；这里主要用于验证/手动整理。
        """
        try:
            body = await request.json() or {}
        except Exception:
            body = {}
        force = bool(body.get("force"))
        keep = int(self.config.get("log_keep_days") or 0)
        result = {"ok": True, "archived": [], "deleted": []}

        def _cut(p):
            """单个文件的切割 + 清理。"""
            if not p:
                return
            try:
                a = (logrot_mod.archive_file(p, logrot_mod.day_str(), keep) if force
                     else logrot_mod.rotate_file_if_stale(p, keep))
                if a:
                    result["archived"].append(a)
                result["deleted"].extend(logrot_mod.prune_archives(p, keep))
            except Exception as e:
                logger.warning("切割 %s 失败: %s", p, e)

        # 运行日志：必须走 handler，否则 handler 还会往被改名后的文件里继续写
        h = self._log_handler()
        if h is not None:
            if force or h.should_rollover():
                result["archived"].append(h.do_rollover())
        else:  # handler 不在本进程（如测试/外部调用）：退化为文件级切割
            _cut(self.config.get("log_path"))
        # 告警 CSV / 重启日志：文件级切割即可
        _cut(self.config.get("alerts_csv_path"))
        _cut(os.path.join(ROOT, "data", "restart.log"))
        logger.info("手动切割日志：归档 %d 个，清理 %d 个",
                    len(result["archived"]), len(result["deleted"]))
        result["files"] = logrot_mod.log_files(self._log_paths())
        return web.json_response(result)

    # ---------- 触发录音 ----------
    def _ensure_recorder(self):
        """采集流就绪后，把原始音频帧接到录音器（只在开启时做一次）。"""
        if self._rec_attached or not self.recorder.enabled():
            return
        self._rec_attached = True       # 无论成败都只试一次，避免每轮刷日志
        try:
            sr, ch = 44100, 1
            try:
                info = self.engine.active_device_info() or {}
                sr = int(info.get("samplerate") or getattr(self.engine, "sample_rate", 0) or 44100)
            except Exception:
                pass
            self.recorder.attach(sr, ch)
            # 报警触发时由循环调用 recorder.cut_at_trigger() 把录音截断在触发瞬间
            # （仅保留触发前音频，绝不录本机放出去的报警声）。这里先按当前配置设好开关。
            self.recorder.set_cut_on_alarm(bool(self.config.get("rec_cut_on_alarm")))
            if hasattr(self.engine, "set_frame_sink"):
                self.engine.set_frame_sink(self.recorder.feed)
                # 模拟引擎要靠 start() 起音频线程；真实引擎的 start() 是幂等的
                try:
                    self.engine.start()
                except Exception as e:
                    logger.debug("接入录音后启动引擎: %s", e)
            logger.info("触发录音已接入：前 %.1f s + 后 %.1f s（触发源=%s）",
                        self.config.get("rec_pre_sec"), self.config.get("rec_post_sec"),
                        self.config.get("rec_trigger"))
        except Exception as e:
            logger.warning("录音器接入失败: %s", e)

    async def get_recordings(self, request):
        """GET /api/recordings：录音开关、缓冲状态、文件数与最近录音列表。"""
        st = self.recorder.stats()
        st.update({
            "ok": True,
            "trigger": self.config.get("rec_trigger"),
            "pre_sec": self.config.get("rec_pre_sec"),
            "post_sec": self.config.get("rec_post_sec"),
            "dir": self.recorder.base_dir(),
            "keep_days": self.config.get("rec_keep_days"),
            "max_files": self.config.get("rec_max_files"),
            "max_mb": self.config.get("rec_max_mb"),
            "cut_on_alarm": bool(self.config.get("rec_cut_on_alarm")),
            "gain_mode": self.config.get("rec_gain_mode") or "off",
            "gain_db": self.config.get("rec_gain_db") or 0.0,
            "alarm_enabled": bool(self.config.get("alarm_enabled")),
        })
        return web.json_response(st)

    async def post_recordings_prune(self, request):
        """POST /api/recordings/prune：立即按保留天数与配额清理录音。"""
        try:
            removed = self.recorder.prune()
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True, "removed": len(removed),
                                  "files": removed[:50], **self.recorder.stats()})

    async def post_recordings_clear(self, request):
        """POST /api/recordings/clear：立即清空所有录音（不可恢复）。"""
        try:
            removed = self.recorder.clear_all()
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True, "removed": removed,
                                  **self.recorder.stats()})

    async def get_audio(self, request):
        """GET /api/audio/<day>/<file>.wav：播放/下载某条录音。

        仅允许读取录音目录内的文件（防目录穿越）。
        """
        tail = request.match_info.get("tail", "")
        base = os.path.abspath(self.recorder.base_dir())
        path = os.path.abspath(os.path.join(base, tail))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return web.Response(status=404, text="录音不存在")
        return web.FileResponse(path, headers={"Content-Type": "audio/wav"})

    # ---------- 每日报警邮件 ----------
    async def get_mail(self, request):
        """GET /api/mail：邮件推送配置（密码脱敏）+ 最近一次发送结果 + 常用邮箱预设。"""
        return web.json_response({"ok": True, **mailer_mod.public_config(self.config)})

    async def post_mail_send(self, request):
        """POST /api/mail/send：立即发送一次报警日报（测试/补发）。

        请求体（均可选）：
          {"day": "2026-09-26"}   指定日期；默认按 mail_day_offset（1=昨天）
          {"dry_run": true}       只生成报表不真发，用于预览
          {"to": "a@b.com"}       临时覆盖收件人
        """
        try:
            body = await request.json() or {}
        except Exception:
            body = {}
        cfg = self.config
        if body.get("to"):
            cfg.set("mail_to", body["to"])
        if body.get("dry_run"):  # 预览：直接把渲染好的正文返回给页面展示
            try:
                rep = mailer_mod.preview(cfg, self.storage, day=body.get("day"))
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)
            return web.json_response({"ok": True, **rep})
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(
                None,
                lambda: mailer_mod.send_daily_report(
                    cfg, self.storage, day=body.get("day"), dry_run=bool(body.get("dry_run"))
                ),
            )
        except Exception as e:
            logger.warning("发送报警日报异常: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        if res.get("ok") and not res.get("skipped") and not body.get("dry_run"):
            cfg.set("mail_last_result", f"{res.get('sent_at', '')} {res['msg']}")
        logger.info("手动发送报警日报：%s", res.get("msg"))
        return web.json_response({"ok": bool(res.get("ok")), **res})

    def _maybe_send_daily_mail(self):
        """每天到点自动发一次（在服务主循环里调用，同步阻塞，故用 executor 跑）。"""
        cfg = self.config
        if not cfg.get("mail_enabled"):
            return
        try:
            due, today = mailer_mod.due_now(cfg)
            if not due or cfg.get("mail_last_sent_day") == today:
                return
            if self._mail_fail_day == today and self._mail_fail_count >= 3:
                return  # 当天已连续失败 3 次，不再重试，避免刷屏
            res = mailer_mod.send_daily_report(cfg, self.storage)
            if res.get("ok"):
                cfg.set("mail_last_sent_day", today)
                cfg.set("mail_last_result", f"{res.get('sent_at', '')} {res['msg']}")
                self._mail_fail_day, self._mail_fail_count = "", 0
            else:
                if self._mail_fail_day != today:
                    self._mail_fail_day, self._mail_fail_count = today, 0
                self._mail_fail_count += 1
                cfg.set("mail_last_result", f"失败({self._mail_fail_count}/3)：{res.get('msg')}")
            logger.info("每日报警邮件（%s）：%s", today, res.get("msg"))
        except Exception as e:
            logger.warning("每日报警邮件异常: %s", e)

    async def get_quiet(self, request):
        """查询报警静音状态（剩余秒数 / 截止时刻）。"""
        return web.json_response(self._quiet_state())

    async def post_quiet(self, request):
        """设置或取消「报警静音时段」。

        请求体三选一：
          {"minutes": 60}   -> 从现在起静音 60 分钟
          {"until_ms": ...} -> 静音到指定 epoch 毫秒时刻
          {"cancel": true}  -> 立即取消静音
        静音只压制**发声**；采样落库与超量告警记录照常进行。
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        now = time.time() * 1000
        if body.get("cancel"):
            self.config.set("quiet_until_ms", 0.0)
            logger.info("已取消报警静音")
            return web.json_response({"ok": True, **self._quiet_state()})

        until = None
        if body.get("minutes") is not None:
            try:
                minutes = float(body["minutes"])
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "minutes 必须是数字"}, status=400)
            if minutes <= 0:
                return web.json_response({"ok": False, "error": "minutes 必须为正数"}, status=400)
            until = now + minutes * 60000.0
        elif body.get("until_ms") is not None:
            try:
                until = float(body["until_ms"])
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "until_ms 必须是数字"}, status=400)
        else:
            return web.json_response(
                {"ok": False, "error": "需要 minutes / until_ms / cancel 之一"}, status=400
            )

        self.config.set("quiet_until_ms", until)
        logger.info("设置报警静音至 %s（%.1f 分钟）",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until / 1000)),
                    (until - now) / 60000.0)
        return web.json_response({"ok": True, **self._quiet_state()})

    def _quiet_state(self):
        now = time.time() * 1000
        until = float(self.config.get("quiet_until_ms") or 0.0)
        remaining = max(0.0, (until - now) / 1000.0) if until > now else 0.0
        return {
            "quiet_until_ms": until,
            "quiet_remaining_sec": round(remaining, 1),
            "quiet_active": remaining > 0,
            "until_text": (
                time.strftime("%H:%M:%S", time.localtime(until / 1000)) if remaining > 0 else ""
            ),
        }

    async def alarm_test(self, request):
        """试听报警音：立即播放约 2 秒报警声，不改变监测状态与静默窗口。

        用于排查"报警音听不到"——与是否超阈值解耦，可独立验证扬声器/播放器是否可用。
        返回当前生效的报警声输出设备，方便用户确认"选的设备"是否真的生效。
        """
        dev = self.alarm.output_device or ""
        ok = self.alarm.test_play(2.0)
        dev_label = dev if dev else "系统默认输出设备"
        msg = "已播放试听音（约 2 秒），应从【%s】发出" % dev_label if ok \
            else "无可用播放器（--fake 模式或无音频输出设备）"
        return web.json_response({"ok": ok, "msg": msg, "device": dev})

    # ---------- 音频设备列表（去重/去噪） ----------
    @staticmethod
    def _want_all_devices(request):
        """是否列出全部设备：URL 参数 ?all=1/0 优先，否则取配置里的 show_all_devices。"""
        q = (request.query.get("all") or "").strip().lower()
        if q in ("1", "true", "yes", "on"):
            return True
        if q in ("0", "false", "no", "off"):
            return False
        return None     # 未指定 -> 由调用方决定是否用配置值

    def _devices_payload(self, kind, include_all=False):
        """组装某方向（input/output）的设备列表响应。

        Windows 下同一物理设备会被 MME/DirectSound/WASAPI/WDM-KS 各列一份，还夹着
        虚拟映射器、回环混音等噪声项；这里默认按物理设备合并去重并剔除噪声项
        （详见 ``audio.enriched_device_list``），用户勾选「显示全部设备」时原样返回。
        """
        # 始终先取全量（含噪声项），再自行筛选：这样 hidden_count 才能反映"被隐藏了多少项"。
        try:
            items = self.engine.device_list(
                kind, self.config.get("device_aliases"), include_all=True)
        except TypeError:            # 演示/测试用的简化引擎不支持 include_all
            items = self.engine.device_list(kind, self.config.get("device_aliases"))
        items = items or []
        hidden = [x for x in items if x.get("hidden")]
        devs = items if include_all else [x for x in items if not x.get("hidden")]
        if not devs and items:
            # 安全兜底：万一某台机器所有设备都被判为噪声项，至少别让用户没得选
            devs = items
        return {
            "devices": devs,
            "hidden_count": 0 if include_all else len(hidden),
            "show_all": bool(include_all),
        }

    async def get_devices(self, request):
        """可用输入设备（麦克风）。默认已合并同一设备的多个 host API 通路。

        查询参数 ``?all=1`` 可临时列出全部（含系统底层端点），不改动配置。
        """
        want = self._want_all_devices(request)
        include_all = bool(self.config.get("show_all_devices")) if want is None else want
        return web.json_response(self._devices_payload("input", include_all))

    async def get_output_devices(self, request):
        """可用**输出**设备（报警声从哪一路播放：3.5mm 耳机 / USB 耳机 / 本机扬声器）。"""
        want = self._want_all_devices(request)
        include_all = bool(self.config.get("show_all_devices")) if want is None else want
        try:
            return web.json_response(self._devices_payload("output", include_all))
        except AttributeError:
            return web.json_response({"devices": [], "hidden_count": 0, "show_all": False})

    async def post_device_alias(self, request):
        """给某个音频设备设/清友好别名（持久化到 config.device_aliases）。

        请求体：``{"name": 设备基础名, "alias": 展示名}``；``alias`` 为空串表示清除该设备别名。
        同名多 host API 副本共用一个基础名，一次命名即可全部生效。
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        name = (data.get("name") or "").strip()
        alias = (data.get("alias") or "").strip()
        if not name:
            return web.json_response({"ok": False, "error": "name required"}, status=400)
        aliases = dict(self.config.get("device_aliases") or {})
        if alias:
            aliases[name] = alias
        else:
            aliases.pop(name, None)
        self.config.set("device_aliases", aliases)
        logger.info("设备别名更新: %s -> %r", name, alias or None)
        return web.json_response({"ok": True, "aliases": aliases})

    def _apply_alias_display(self, items):
        """给设备条目补上友好名（子进程探针拿不到服务端配置，需在这里回填）。"""
        aliases = self.config.get("device_aliases") or {}
        if not aliases:
            return items
        for x in items or []:
            nm = x.get("name") or ""
            base = nm.split(",")[0].strip()
            alias = aliases.get(x.get("group") or "") or aliases.get(base) or aliases.get(nm) or ""
            if alias:
                x["display"] = alias
                x["alias"] = alias
        return items

    async def post_devices_rescan(self, request):
        """热插拔后重新枚举音频设备——**用子进程探针，不动本进程的 PortAudio**。

        老做法是在本进程里 ``sd._terminate()/_initialize()``，实测会打乱内部 host API 的
        索引映射：之后 MME 设备报 ``device ID out of range``，被迫改走 DirectSound
        （本机实测那条通路完全静音），读数对该设备之前做的校准直接失效——
        这正是"换个输入设备后就不准"的元凶。

        现在改由 ``volmon.probe`` 子进程枚举（它自带干净的 PortAudio，能看到新插入的
        设备），本进程正在跑的采集流**不停、不重排**，读数不漂。
        若子进程看到本进程枚举不到的设备，则返回 ``need_restart`` 提示，让用户点
        「重启服务」后真正使用新设备（重启是彻底干净的方式，代价仅约 2 秒）。
        """
        include_all = bool(self.config.get("show_all_devices"))
        aliases = self.config.get("device_aliases") or {}
        probe = getattr(self.engine, "probe_subprocess", None)
        probe_result = probe() if callable(probe) else {"ok": False, "error": "unsupported"}

        if probe_result.get("ok"):
            devices = probe_result.get("devices") or []
            output_devices = probe_result.get("output_devices") or []
            self._apply_alias_display(devices + output_devices)
            # 本进程此刻能看到的设备组（用于判断"新设备能不能立刻用上"）
            mine = set()
            for kind in ("input", "output"):
                mine |= {x.get("group") for x in self._devices_payload(kind, True)["devices"]}
            new_groups = [x.get("group") for x in devices + output_devices
                          if x.get("group") and x.get("group") not in mine]
            need_restart = bool(new_groups)
            logger.info("子进程探针枚举：输入 %d 个 / 输出 %d 个；本进程暂不可用的新设备组 %s",
                        len(devices), len(output_devices), new_groups or "无")
            return web.json_response({
                "ok": True,
                "devices": devices,
                "output_devices": output_devices,
                "hidden_count": int(probe_result.get("hidden_input") or 0)
                                + int(probe_result.get("hidden_output") or 0),
                "show_all": include_all,
                "need_restart": need_restart,
                "pending_devices": sorted(set(new_groups)),
                "msg": ("发现本进程暂时无法直接使用的设备，点「重启服务」后即可选择："
                        + "、".join(sorted(set(new_groups)))) if need_restart else "",
            })

        # 子进程探针不可用（如打包环境）→ 回退到旧的进程内重扫，但明确警告副作用
        logger.warning("子进程探针失败(%s)，回退到进程内 PortAudio 重初始化（可能使读数漂移，"
                       "必要时请重启服务）", probe_result.get("error"))
        try:
            inputs = self.engine.rescan()
        except AttributeError:
            return web.json_response(
                {"ok": False, "msg": "当前引擎不支持重新扫描（演示模式）"}, status=400
            )
        except Exception as e:
            logger.warning("重新扫描音频设备失败: %s", e)
            return web.json_response({"ok": False, "msg": f"重新扫描失败: {e}"}, status=500)
        ins = self._devices_payload("input", include_all)
        outs = self._devices_payload("output", include_all)
        logger.info("已重新扫描音频设备：输入 %d 个 / 输出 %d 个",
                    len(ins["devices"]), len(outs["devices"]))
        return web.json_response({
            "ok": True,
            "devices": ins["devices"],
            "output_devices": outs["devices"],
            "hidden_count": ins["hidden_count"] + outs["hidden_count"],
            "show_all": include_all,
            "need_restart": True,
            "pending_devices": [],
            "msg": "已用进程内重扫（可能让当前读数偏移），建议点「重启服务」后重新校准",
        })

    async def get_raw_avg(self, request):
        """返回原始均值，供校准计算使用。

        raw_avg    : dBFS 原始均值（<=0）——已改为**能量域 RMS**（见 audio.rms_db_over），
                     与专业声级计同一口径；旧接口 averaging 的是 dB 值（对数域），
                     起伏大时会系统性低估数 dB，导致"校准完读数仍对不上"。
        base_avg   : raw_avg + ref_spl_offset_db，即"未校准时界面显示的 dB SPL 估算值"
        displayed  : 当前实际显示值（base_avg + 当前设备生效的校准偏移）
        校准时用 base_avg，使"实测值 - base_avg"即为应追加的校准修正量。
        """
        n = request.query.get("n")
        # 兼容旧的 n=<块数> 参数：块数 × 每块时长 ≈ 秒数（44.1kHz/2048 块 ≈ 46ms）
        if n:
            try:
                seconds = max(float(n) * 0.0464, 0.1)
            except ValueError:
                seconds = 5.0
        else:
            seconds = 5.0
        return web.json_response(self._calib_payload(seconds))

    # ---------- 校准：按设备隔离 ----------
    def _active_calib_device(self):
        """当前**实际在采集**的麦克风全名（拿不到运行时信息就退回配置值）。"""
        info = self._input_dev_info or {}
        if isinstance(info, dict) and info.get("name"):
            return info["name"]
        dev = getattr(self.engine, "device", None)
        if isinstance(dev, str) and dev:
            return dev
        return self.config.get("device") or "default"

    def _active_hostapi_slug(self):
        """当前采集流实际所在通路的短标识（mme / directsound / wasapi / wdm-ks）。"""
        info = self._input_dev_info or {}
        if isinstance(info, dict) and info.get("hostapi_slug"):
            return info["hostapi_slug"]
        getter = getattr(self.engine, "stream_path", None)
        if callable(getter):
            try:
                return (getter() or {}).get("hostapi_slug") or ""
            except Exception:
                return ""
        return ""

    def calib_state(self):
        """当前生效的校准偏移 ``(offset, key, device, source)``。

        ``source``（可靠度从高到低）：
          * ``path``     —— 这台麦克风**并且就是当前这条 host API 通路**校过，最可靠
          * ``device``   —— 这台麦克风校过，但当时走的是**另一条通路**；本机实测
                            麦克风阵列走 MME 与 DirectSound 相差极大（后者甚至完全静音），
                            此时读数可能偏差，面板会提示重新校准
          * ``global``   —— 没校过，退回旧配置的全局 ``calib_offset_db``（兼容老配置）
          * ``estimate`` —— 都没有：未校准，读数只是 ``ref_spl_offset_db`` 粗估

        ``key`` 形如 ``麦克风阵列@mme``（物理设备组键 + 通路）；查不到精确条目时
        会退到纯组键 ``麦克风阵列``，再没有才走全局值。
        """
        table = self.config.get("device_calib") or {}
        name = self._active_calib_device()
        key = device_group_key(name) if name else "default"
        slug = self._active_hostapi_slug()
        path_key = f"{key}@{slug}" if slug else key
        g = float(self.config.get("calib_offset_db") or 0)
        if isinstance(table, dict):
            if slug and path_key in table:
                try:
                    return float(table[path_key]), path_key, name, "path"
                except (TypeError, ValueError):
                    pass
            if key in table:
                try:
                    return float(table[key]), path_key, name, "device"
                except (TypeError, ValueError):
                    pass
        return g, path_key, name, ("global" if abs(g) > 1e-6 else "estimate")

    def _meter_raw(self, seconds=None):
        """最近 ``seconds`` 秒的能量域 RMS（dBFS）；旧引擎没有该接口时退回瞬时值。"""
        if seconds is None:
            seconds = float(self.config.get("meter_smooth_sec") or 0)
        getter = getattr(self.engine, "rms_db_over", None)
        if callable(getter):
            return float(getter(seconds))
        return float(self.engine.raw_db())

    def _calib_payload(self, seconds=5.0):
        """构造校准面板/接口用的统一载荷（当前设备 + 通路 + 生效偏移 + 三种读数）。"""
        raw = self._meter_raw(seconds)
        ref = float(self.config.get("ref_spl_offset_db") or 0)
        off, path_key, dev, src = self.calib_state()
        path = self._active_path_info()
        return {
            "raw_avg": round(raw, 2),
            "ref_offset_db": ref,
            "base_avg": round(raw + ref, 2),
            "displayed": round(max(raw + ref + off, 0.0), 2),
            "device": dev,
            "device_key": path_key,
            "offset": round(off, 2),
            "source": src,
            # 有本设备及上层（精确通路/整机）任一记录即算"已校准"；
            # path_exact=False 表示用的是另一条通路校准出来的值，读数可能偏差。
            "calibrated": src in ("path", "device"),
            "path_exact": src == "path",
            "hostapi": path.get("hostapi"),
            "hostapi_slug": path.get("hostapi_slug") or "",
            "fallback": bool(path.get("fallback")),
            "fallback_reason": path.get("fallback_reason") or "",
            "window_sec": round(float(seconds), 2),
            # 麦克风没信号时算出的偏移是假的（通路选错/被静音/权限未开）
            "silent": bool(raw <= DBFS_FLOOR + 1.0),
            "device_calib": dict(self.config.get("device_calib") or {}),
        }

    def _active_path_info(self):
        """当前采集流的通路信息（含是否并非首选通路）。"""
        info = self._input_dev_info or {}
        if isinstance(info, dict) and (info.get("hostapi") or info.get("fallback")):
            return {
                "index": info.get("index"),
                "name": info.get("name"),
                "hostapi": info.get("hostapi"),
                "hostapi_slug": info.get("hostapi_slug") or "",
                "fallback": bool(info.get("fallback")),
                "fallback_reason": info.get("fallback_reason") or "",
            }
        getter = getattr(self.engine, "stream_path", None)
        if callable(getter):
            try:
                return getter() or {}
            except Exception:
                pass
        return {}

    async def get_calib(self, request):
        """GET /api/calib：当前设备的校准状态 + 即时读数（``?window=5`` 可指定取样秒数）。"""
        raw_win = request.query.get("window")
        if raw_win:
            try:
                seconds = max(float(raw_win), 0.1)
            except ValueError:
                seconds = 5.0
        else:
            seconds = max(float(self.config.get("meter_smooth_sec") or 0), 5.0)
        return web.json_response({"ok": True, **self._calib_payload(seconds)})

    async def post_calib(self, request):
        """POST /api/calib：按**当前设备**写入/清除校准偏移。

        请求体：
          ``{"real_db": 65.0}``              按专业设备实测值校准（默认取最近 5 秒能量 RMS），
                                             并**绑定到当前这条 host API 通路**（推荐）
          ``{"real_db": 65.0, "window": 10}``用更长/更短的取样窗口
          ``{"real_db": 65.0, "scope": "device"}``  绑定到整台设备（所有通路共用该偏移）
          ``{"clear": true}``                清除**当前通路**的校准记录（回退到整机/估算）
        """
        try:
            body = await request.json() or {}
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        try:
            seconds = max(float(body.get("window") or 5.0), 0.1)
        except (TypeError, ValueError):
            seconds = 5.0
        _off, path_key, dev, _src = self.calib_state()
        group_key = path_key.split("@", 1)[0]
        scope = (body.get("scope") or "path").lower()

        if body.get("clear"):
            table = dict(self.config.get("device_calib") or {})
            if scope == "device":
                # 清整台设备：删掉它的所有通路条目
                for k in [k for k in table if k == group_key or k.startswith(group_key + "@")]:
                    table.pop(k, None)
            else:
                table.pop(path_key, None)
            self.config.set("device_calib", table)
            logger.info("已清除 %r 的校准记录（scope=%s）", dev, scope)
            return web.json_response({"ok": True, "cleared": True,
                                      **self._calib_payload(seconds)})

        real = body.get("real_db", body.get("real"))
        if real is None:
            return web.json_response({"ok": False, "error": "缺少 real_db"}, status=400)
        try:
            real_db = float(real)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "real_db 必须是数字"}, status=400)
        if not 0.0 <= real_db <= 140.0:
            return web.json_response({"ok": False, "error": "real_db 应在 0~140 dB 之间"},
                                     status=400)

        payload = self._calib_payload(seconds)
        if payload["silent"]:
            return web.json_response(
                {"ok": False,
                 "error": "麦克风当前无有效信号（读数已到静音地板），校准结果会失真。"
                          "请确认设备未被静音、音量不为 0，然后重试。"},
                status=400)
        base = payload["base_avg"]
        offset = round(real_db - base, 2)
        if body.get("scope") == "global":
            self.config.set("calib_offset_db", offset)
        else:
            table = dict(self.config.get("device_calib") or {})
            # 默认按"设备 + 当前通路"保存；同一麦克风走不同 host API 时电平可能相差
            # 十几 dB，混用会让读数失准——分通路保存可彻底避免这类串味。
            table[group_key if scope == "device" else path_key] = offset
            self.config.set("device_calib", table)
        logger.info("校准 [%s | %s | %s]: 实测 %.1f dB / 基准 %.2f dB / 修正 %+.2f dB",
                    dev, path_key, scope, real_db, base, offset)
        out = self._calib_payload(seconds)
        out.update({"ok": True, "applied": offset, "real_db": real_db})
        return web.json_response(out)

    # ---------- WebSocket ----------
    async def handle_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        self.clients.add(ws)
        logger.info("WS 客户端接入，当前 %d", len(self.clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    if data.get("type") == "set_config":
                        applied = {}
                        for k, v in data.get("values", {}).items():
                            if k in CONFIG_KEYS:
                                self.config.set(k, v)
                                applied[k] = self.config.get(k)
                        self.alerting.update_params(
                            self.config.get("threshold_db"), self.config.get("sustain_sec")
                        )
                        self._sync_log_retention()
                        await ws.send_str(json.dumps({"type": "config_applied", "applied": applied}))
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WS 错误: %s", ws.exception())
        finally:
            self.clients.discard(ws)
            logger.info("WS 客户端断开，当前 %d", len(self.clients))
        return ws

    # ---------- 输入设备热切换 ----------
    def _sync_input_device(self):
        """配置里的输入设备变了就即时切换采集流（InputStream 是启动时打开的，改配置本不会生效）。"""
        dev = self.config.get("device") or "default"
        if dev == self._cur_input_device:
            return
        if not hasattr(self.engine, "switch_device"):
            return
        if self._cur_input_device is None:
            # 首次：服务启动时已按配置打开了采集流，只记录不做无谓重启
            try:
                self._input_dev_info = self.engine.active_device_info()
            except Exception:
                self._input_dev_info = None
            self._cur_input_device = dev
            return
        try:
            self._input_dev_info = self.engine.switch_device(dev)
        except Exception as e:
            logger.warning("切换输入设备到 %r 失败: %s", dev, e)
            try:
                self._input_dev_info = self.engine.active_device_info()
            except Exception:
                self._input_dev_info = None
        # 无论成功失败都记为"已处理"，避免每 100ms 反复重启采集流
        self._cur_input_device = dev

    async def broadcast(self, obj):
        if not self.clients:
            return
        text = json.dumps(obj, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ---------- 后台采集广播循环 ----------
    async def loop(self):
        while True:
            try:
                self._ensure_recorder()
                now = time.time() * 1000
                # 分时段策略：算出当前时段（白天/晚间）的**有效配置**
                sched = schedule_mod.resolve(self.config, now / 1000.0)
                eff = sched["config"]
                self.alarm.sync_from_config(eff)
                # 输入设备热切换：改了配置立刻生效，无需重启服务
                self._sync_input_device()
                # 注意读数的统计口径：默认取最近一小段时间的**能量 RMS**（meter_smooth_sec，
                # 近似声级计 Fast 档），而不是单个音频块的瞬时值——瞬时值在起伏大的环境里
                # 会明显高于"听感音量"，且与声级计读数对不齐；设 0 可恢复瞬时。
                raw = self._meter_raw()
                ref = float(self.config.get("ref_spl_offset_db"))   # dBFS→dB SPL 参考偏移
                # 校准修正：优先用**当前这台麦克风**自己的偏移（device_calib），
                # 没有才退回旧的全局值；换设备/拔插耳麦不会串用到别的设备的偏移。
                offset, _ckey, _cdev, csrc = self.calib_state()
                threshold = float(eff["threshold_db"])
                # 阈值/持续时长按时段生效（仅在变化时同步，避免每轮无谓更新）
                sustain = float(eff["sustain_sec"])
                if (threshold, sustain) != self._alerting_params:
                    self.alerting.update_params(threshold, sustain)
                    self._alerting_params = (threshold, sustain)
                    logger.info(
                        "生效策略[%s%s]: 阈值 %.1f dB / 持续 %.1fs / 声音报警 %s%s / 输出 %s",
                        sched["period_label"],
                        "（分时段）" if sched["applied"] else "（全局）",
                        threshold, sustain,
                        "开" if eff.get("alarm_enabled") else "关",
                        f" {eff.get('alarm_duration_sec')}s（冷却 {eff.get('alarm_cooldown_sec')}s）"
                        if eff.get("alarm_enabled") else "",
                        eff.get("alarm_output_device") or "系统默认输出",
                    )
                # 把"当前生效策略"快照交给告警器，写入每条超量告警记录
                policy = {
                    "period": sched["period_label"],
                    "schedule_applied": sched["applied"],
                    "alarm_enabled": bool(eff.get("alarm_enabled")),
                    "alarm_duration_sec": float(eff.get("alarm_duration_sec") or 0),
                    "alarm_cooldown_sec": float(eff.get("alarm_cooldown_sec") or 0),
                }
                pkey = tuple(sorted(policy.items()))
                if pkey != self._policy_key:
                    self.alerting.set_context(policy)
                    self._policy_key = pkey
                # 声压级估算：dBFS + 参考偏移 + 校准修正；并钳制为非负（真实声压级不会为负）
                db = float(max(raw + ref + offset, 0.0))
                over = bool(db > threshold)

                # 报警播放/冷却期间：整体静默，不记录、不触发噪声预警
                phase = self.alarm.phase(now)
                suppressed = phase in ("playing", "cooldown")

                alarm_fired = False
                if not suppressed:
                    state = self.alerting.check(db, now)
                    # 按频率落库（后台详细采样日志）
                    log_interval = self.config.get("log_interval_ms")
                    if now - self._last_log_ts >= log_interval:
                        self.storage.add_sample(
                            int(now), round(raw, 2), round(db, 2), self.config.get("device")
                        )
                        self._last_log_ts = now
                    # 持续超阈值 -> 触发电脑音量报警（返回 True 表示本次实际启动了报警声）
                    if self.alarm.enabled and state.get("alert"):
                        alarm_fired = self.alarm.trigger(now)
                    alert = bool(state.get("alert"))
                    peak = round(float(state.get("peak", 0.0)), 2)
                else:
                    alert = False
                    peak = 0.0

                # 录音：边沿触发——只在"刚进入告警/刚超阈值"那一刻开录，
                # 前置的几秒由常驻环形缓冲补齐，所以声音再短也不会丢开头
                # 不论是否处于报警静默期，都按当前配置同步"报警触发即截断"开关，
                # 保证运行期行为与用户保存的开关一致（不受 suppressed 影响）
                self.recorder.set_cut_on_alarm(bool(self.config.get("rec_cut_on_alarm")))
                if not suppressed and self.recorder.enabled():
                    mode = self.config.get("rec_trigger") or "alert"
                    reason = None
                    if mode == "over" and over and not self._prev_over:
                        reason = "超阈值"
                    elif mode == "alert" and alert and not self._prev_alert:
                        reason = "报警"
                    if reason:
                        try:
                            self.recorder.trigger(peak_db=db, reason=reason)
                        except Exception as e:
                            logger.warning("启动录音失败: %s", e)
                self._prev_alert = alert
                self._prev_over = over

                # 报警声触发 → 立即把正在进行的录音截断在触发瞬间
                # （仅保留"触发前"那段真实环境音，触发点之后的报警声一律不录，详见 recorder 文档）
                if alarm_fired and self.recorder.cut_on_alarm():
                    try:
                        self.recorder.cut_at_trigger(reason="报警触发·仅保留触发前")
                    except Exception as e:
                        logger.warning("报警截断录音失败: %s", e)

                # 录音收尾：录够时长就写盘，并把文件名回填到对应那条告警
                try:
                    for rel, _path, meta in self.recorder.tick():
                        self.storage.link_audio(meta.get("trigger_ts", int(now)), rel)
                except Exception as e:
                    logger.warning("录音收尾失败: %s", e)

                try:
                    mic_silent = bool(self.engine.is_silent())
                except Exception:
                    mic_silent = False

                quiet_left = self.alarm.quiet_remaining_sec(now)
                msg = {
                    "t": int(now),
                    "raw_db": round(raw, 2),
                    "db": round(db, 2),
                    "threshold": threshold,
                    "over": over,
                    "alert": alert,
                    "peak": peak,
                    "calibrated": csrc in ("path", "device"),
                    "calib_source": csrc,
                    "calib_exact": csrc == "path",
                    "alarm_phase": phase,
                    "suppressed": suppressed,
                    "mic_silent": mic_silent,
                    "version": __version__,
            "product": __product__,
            "version_display": __display__,
                    # 实际在用的输入设备（用于确认"到底在用哪个麦克风"）与报警输出设备
                    "input_device": self._input_dev_info,
                    "alarm_output_device": self.config.get("alarm_output_device") or "",
                    # 分时段策略：当前时段与是否按分时段生效
                    "period": sched["period"],
                    "period_label": sched["period_label"],
                    "schedule_applied": sched["applied"],
                    # 报警静音时段：仅不发声，监测与超量记录照常（suppressed 仍为 False）
                    "quiet_remaining_sec": round(quiet_left, 1),
                    "quiet_active": bool(quiet_left > 0),
                }
                await self.broadcast(msg)
                # 每日报警邮件：每 30 秒检查一次是否到点（放 executor，避免阻塞采集广播）
                if now - self._last_mail_check >= 30_000:
                    self._last_mail_check = now
                    if self.config.get("mail_enabled"):
                        await asyncio.get_running_loop().run_in_executor(
                            None, self._maybe_send_daily_mail
                        )
            except Exception as e:
                logger.warning("广播循环异常: %s", e)
            await asyncio_sleep(self.config.get("sample_interval_ms") / 1000.0)


def asyncio_sleep(sec):
    import asyncio

    return asyncio.sleep(sec)


# ---------- 历史趋势导出（CSV：统计汇总 + 字符趋势图 + 预警明细 + 原始数据） ----------

_TREND_BLOCKS = "▁▂▃▄▅▆▇█"     # 迷你柱状图字符（由低到高 8 级）
_TREND_MAX_ROWS = 50000        # 原始明细行数上限，超出则均匀抽稀，避免导出巨型文件


def _fmt_ts(ms):
    """毫秒时间戳 -> 本地时间字符串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000.0))


def _mini_bar(v, lo, hi):
    """单点迷你柱状图：把 v 在 [lo,hi] 内归一化后用 Unicode 块字符表示高度。

    Excel 打开 CSV 时这一列会呈现柱状高低起伏，即"每行数据自带一个迷你图"。
    """
    if hi <= lo:
        return _TREND_BLOCKS[3]
    r = (v - lo) / (hi - lo)
    r = 0.0 if r < 0 else (1.0 if r > 1 else r)
    return _TREND_BLOCKS[int(r * (len(_TREND_BLOCKS) - 1) + 0.5)]


def _ascii_trend(pts, threshold, cols=100, lines=18):
    """把采样点渲染成字符折线图：* = 该列时段的采样均值，- = 预警阈值线。"""
    if not pts:
        return ["（该时段暂无采样数据）"]
    t0, t1 = pts[0][0], pts[-1][0]
    lo = min(d for _, d in pts)
    hi = max(d for _, d in pts)
    if threshold:
        lo, hi = min(lo, threshold), max(hi, threshold)
    if hi - lo < 10:            # 动态范围过窄时上下各让 5 dB，避免曲线被压成一条直线
        mid = (hi + lo) / 2.0
        lo, hi = mid - 5, mid + 5
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    span = max(1e-6, float(t1 - t0))
    buckets = [[] for _ in range(cols)]
    for ts, d in pts:
        i = int((ts - t0) / span * (cols - 1))
        buckets[max(0, min(cols - 1, i))].append(d)
    grid = [[" "] * cols for _ in range(lines)]
    for i, b in enumerate(buckets):
        if not b:
            continue
        v = sum(b) / len(b)
        r = int((hi - v) / (hi - lo) * (lines - 1) + 0.5)
        grid[max(0, min(lines - 1, r))][i] = "*"
    if threshold:
        r = max(0, min(lines - 1, int((hi - threshold) / (hi - lo) * (lines - 1) + 0.5)))
        for i in range(cols):
            if grid[r][i] == " ":
                grid[r][i] = "-"
    out = []
    for r in range(lines):
        db = hi - (hi - lo) * r / (lines - 1)
        out.append("%5.1f dB |%s" % (db, "".join(grid[r])))
    out.append(" " * 10 + "+" + "-" * cols)
    a, b = _fmt_ts(t0), _fmt_ts(t1)
    out.append(" " * 11 + a + " " * max(1, cols - len(a) - len(b)) + b)
    return out


def build_trend_csv(samples, alerts, threshold, hours_label, since_ms, now_ms):
    """生成历史趋势 CSV：统计汇总 + 字符趋势图 + 预警明细 + 原始数据（含迷你图列）。"""
    import csv
    import io
    import math

    pts = [(s["t"], s["db"]) for s in samples]
    dbs = [d for _, d in pts]
    n = len(dbs)
    avg = sum(dbs) / n if n else 0.0
    mx = max(dbs) if n else 0.0
    mn = min(dbs) if n else 0.0
    over = sum(1 for d in dbs if threshold and d > threshold)
    durs = [a["duration_s"] for a in alerts if a.get("duration_s")]
    total_dur = sum(durs)

    thin = n > _TREND_MAX_ROWS
    step = max(1, int(math.ceil(n / float(_TREND_MAX_ROWS)))) if thin else 1
    detail = samples[::step]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["volmon 历史趋势导出"])
    w.writerow(["导出时间", _fmt_ts(now_ms)])
    w.writerow(["统计区间", hours_label])
    w.writerow(["时间范围", "%s ~ %s" % (_fmt_ts(since_ms), _fmt_ts(now_ms))])
    w.writerow(["预警阈值(dB)", "%.1f" % threshold])
    if thin:
        w.writerow(["原始数据抽稀", "该时段共 %d 条采样，超过 %d 条上限，"
                    "原始明细已按每 %d 条均匀抽样为 %d 条" % (n, _TREND_MAX_ROWS, step, len(detail))])
    w.writerow([])

    w.writerow(["统计汇总"])
    w.writerow(["指标", "数值"])
    w.writerow(["采样点数", n])
    w.writerow(["均值 dB", "%.1f" % avg])
    w.writerow(["峰值 dB", "%.1f" % mx])
    w.writerow(["超阈值采样点占比", "%.1f%%（%d/%d）" % (over / n * 100 if n else 0.0, over, n)])
    w.writerow(["预警次数", len(alerts)])
    w.writerow(["预警峰值 dB", "%.1f" % max((a["peak_db"] for a in alerts), default=0.0)])
    w.writerow(["预警总时长(秒)", "%.1f" % total_dur])
    w.writerow(["最长单次预警(秒)", "%.1f" % max(durs, default=0.0)])
    w.writerow(["平均单次预警(秒)", "%.1f" % (total_dur / len(durs) if durs else 0.0)])
    w.writerow([])

    w.writerow(["趋势图（* = 该列时段内的采样均值；- = 预警阈值线；左列为 dB 刻度）"])
    for line in _ascii_trend(pts, threshold):
        w.writerow([line])
    w.writerow([])

    w.writerow(["预警明细"])
    w.writerow(["开始时间", "结束时间", "持续(秒)", "峰值dB", "阈值dB",
                "时段", "分时段", "声音报警", "录音文件"])
    for a in alerts:
        w.writerow([
            _fmt_ts(a["start_ts"]), _fmt_ts(a["end_ts"]), a["duration_s"],
            a["peak_db"], a["threshold_db"], a["period"] or "全局",
            "是" if a["schedule_applied"] else "否",
            ("开 %ss" % a["alarm_duration_sec"]) if a["alarm_enabled"] else "关",
            a["audio_file"] or "",
        ])
    w.writerow([])

    w.writerow(["原始数据"])
    w.writerow(["时间", "dB", "原始dBFS", "是否超阈值", "迷你趋势图"])
    for s in detail:
        w.writerow([
            _fmt_ts(s["t"]), "%.1f" % s["db"], "%.1f" % s["raw_db"],
            "是" if (threshold and s["db"] > threshold) else "否",
            _mini_bar(s["db"], mn, mx),
        ])
    # 加 BOM：保证 Excel 双击打开时中文不乱码
    return "\ufeff" + buf.getvalue()


def make_app(engine, storage, config, alerting):
    srv = MonitorServer(engine, storage, config, alerting)
    return srv
