"""后端服务：aiohttp 提供 WebSocket 实时广播、REST 配置/采样/告警/设备接口、托管 Web 页面。"""
import asyncio
import json
import logging
import os
import time

from aiohttp import web, WSMsgType
from volmon import __display__, __product__, __version__
from volmon import alarm as alarm_mod
from volmon import logrot as logrot_mod
from volmon import mailer as mailer_mod
from volmon import paths as paths_mod
from volmon import recorder as recorder_mod
from volmon import schedule as schedule_mod
from volmon import storage_quota as quota_mod

logger = logging.getLogger("volmon.server")

# 数据/日志根目录：源码运行=项目根；打包后=~/Library/Application Support/volmon-mac
ROOT = paths_mod.data_root()

# 允许网页/WS 修改的配置项（其余为内部项，避免误改）
CONFIG_KEYS = {
    "threshold_db",
    "sustain_sec",
    "sample_interval_ms",
    "log_interval_ms",
    "calib_offset_db",
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
    "storage_max_mb",
    "rec_cut_on_alarm",
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
        self._last_quota_check = 0.0    # 上次检查"日志+录音合计配额"的时刻
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
        self.app.router.add_get("/api/config", self.get_config)
        self.app.router.add_post("/api/config", self.post_config)
        self.app.router.add_get("/api/samples", self.get_samples)
        self.app.router.add_get("/api/alerts", self.get_alerts)
        self.app.router.add_get("/api/alerts/count", self.get_alerts_count)
        self.app.router.add_get("/api/devices", self.get_devices)
        self.app.router.add_post("/api/devices/rescan", self.post_devices_rescan)
        self.app.router.add_get("/api/output-devices", self.get_output_devices)
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
        self.app.router.add_get("/api/storage/quota", self.get_storage_quota)
        self.app.router.add_post("/api/storage/quota/enforce", self.post_storage_quota_enforce)
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

    async def post_config_reset(self, request):
        """一键恢复默认设置：把 data/config.json 还原为 DEFAULTS（含阈值/校准/报警/设备）。"""
        cfg = self.config.reset()
        self.alerting.update_params(
            self.config.get("threshold_db"), self.config.get("sustain_sec")
        )
        logger.info("配置已恢复默认")
        return web.json_response({"ok": True, "config": cfg})

    async def post_service_restart(self, request):
        """重启服务：派生接替进程后自行退出（用于换设备/改代码后生效）。"""
        try:
            from volmon import restart as restart_mod

            keep = int(self.config.get("log_keep_days") or 0) if self.config.get("log_rotate_daily") else 0
            ok, msg = restart_mod.spawn_restart(
                ROOT, os.path.join(ROOT, "data", "restart.log"), keep_days=keep
            )
        except Exception as e:
            return web.json_response({"ok": False, "msg": f"重启失败: {e}"}, status=500)
        if not ok:
            return web.json_response({"ok": False, "msg": msg}, status=500)
        logger.info("收到重启指令：%s", msg)
        # 响应返回后再退出，确保前端收到回执
        asyncio.get_running_loop().create_task(self._exit_after(0.8))
        return web.json_response({"ok": True, "msg": "服务正在重启，约 2 秒后请刷新页面"})

    async def post_service_stop(self, request):
        """停止服务。

        桌面版（.app）没有窗口也没有菜单栏，双击后看不出进程在哪——
        这个接口给网页上的「停止服务」按钮用，让用户可以正常退出程序。
        """
        logger.info("收到停止指令：服务即将退出")
        asyncio.get_running_loop().create_task(self._exit_after(0.8))
        return web.json_response({"ok": True, "msg": "服务已停止（桌面版即退出程序）"})

    async def _exit_after(self, delay):
        import asyncio as _a

        await _a.sleep(delay)
        os._exit(0)

    async def get_status(self, request):
        """排障用：返回**实际在用的**输入设备与报警输出设备。"""
        try:
            info = self.engine.active_device_info()
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
        return web.json_response({"ok": True, "applied": applied,
                                  **schedule_mod.status(self.config)})

    # ---------- 当前生效策略明细 ----------
    def policy_snapshot(self):
        """把"此刻真正生效的一整套策略"汇总成一目了然的明细。"""
        st = schedule_mod.status(self.config)
        try:
            dev = self.engine.active_device_info()
        except Exception:
            dev = None
        q = self._quiet_state()
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
            "calibrated": abs(float(self.config.get("calib_offset_db") or 0)) > 1e-6,
            "calib_offset_db": float(self.config.get("calib_offset_db") or 0),
            "ref_spl_offset_db": float(self.config.get("ref_spl_offset_db") or 0),
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

    # ---------- 统一存储配额（日志 + 录音）----------
    async def get_storage_quota(self, request):
        """GET /api/storage/quota：日志+录音合计占用与上限。"""
        return web.json_response(quota_mod.stats(self.config))

    async def post_storage_quota_enforce(self, request):
        """POST /api/storage/quota/enforce：立即按配额清理（删最旧）。"""
        try:
            removed = quota_mod.enforce(self.config)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({
            "ok": True,
            "removed": len(removed),
            "files": [r[0] for r in removed[:50]],
            **quota_mod.stats(self.config),
        })

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
        """
        ok = self.alarm.test_play(2.0)
        msg = "已播放试听音（约 2 秒）" if ok else "无可用播放器（--fake 模式或无音频输出设备）"
        return web.json_response({"ok": ok, "msg": msg})

    async def get_devices(self, request):
        devs = self.engine.list_input_devices()
        return web.json_response(
            [{"index": d[0], "name": d[1], "channels": d[2]} for d in devs]
        )

    async def get_output_devices(self, request):
        """可用**输出**设备（报警声从哪一路播放：3.5mm 耳机 / USB 耳机 / 本机扬声器）。"""
        try:
            devs = self.engine.list_output_devices()
        except AttributeError:
            devs = []
        return web.json_response(
            [{"index": d[0], "name": d[1], "channels": d[2]} for d in devs]
        )

    async def post_devices_rescan(self, request):
        """热插拔后重新枚举音频设备（**无需重启服务**）。

        PortAudio 在进程内只枚举一次，插入 USB 声卡/耳机后不重新初始化就看不到新设备。
        会短暂中断采集流（停止 → 重初始化 → 恢复），返回最新的输入/输出设备列表。
        """
        try:
            inputs = self.engine.rescan()
        except AttributeError:
            return web.json_response(
                {"ok": False, "msg": "当前引擎不支持重新扫描（演示模式）"}, status=400
            )
        except Exception as e:
            logger.warning("重新扫描音频设备失败: %s", e)
            return web.json_response({"ok": False, "msg": f"重新扫描失败: {e}"}, status=500)
        try:
            outputs = self.engine.list_output_devices()
        except AttributeError:
            outputs = []
        logger.info("已重新扫描音频设备：输入 %d 个 / 输出 %d 个", len(inputs), len(outputs))
        return web.json_response({
            "ok": True,
            "devices": [{"index": d[0], "name": d[1], "channels": d[2]} for d in inputs],
            "output_devices": [
                {"index": d[0], "name": d[1], "channels": d[2]} for d in outputs
            ],
        })

    async def get_raw_avg(self, request):
        """返回原始均值，供校准计算使用。

        raw_avg    : dBFS 原始均值（<=0）
        base_avg   : raw_avg + ref_spl_offset_db，即"未校准时界面显示的 dB SPL 估算值"
        displayed  : 当前实际显示值（base_avg + calib_offset_db）
        校准时用 base_avg，使"实测值 - base_avg"即为应追加的校准修正量。
        """
        n = int(request.query.get("n", 100))
        raw_avg = round(float(self.engine.recent_raw_avg(n)), 2)
        ref = float(self.config.get("ref_spl_offset_db"))
        off = float(self.config.get("calib_offset_db"))
        return web.json_response(
            {
                "raw_avg": raw_avg,
                "ref_offset_db": ref,
                "base_avg": round(raw_avg + ref, 2),
                "displayed": round(max(raw_avg + ref + off, 0.0), 2),
            }
        )

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
                raw = float(self.engine.raw_db())          # dBFS（<=0，满刻度为 0）
                ref = float(self.config.get("ref_spl_offset_db"))   # dBFS→dB SPL 参考偏移
                offset = float(self.config.get("calib_offset_db"))  # 专业设备校准修正
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
                    "calibrated": abs(offset) > 1e-6,
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
                # 统一存储配额：每 5 分钟检查一次，日志+录音合计超上限则从最旧开始删
                if now - self._last_quota_check >= 5 * 60_000:
                    self._last_quota_check = now
                    try:
                        removed = quota_mod.enforce(self.config)
                        if removed:
                            logger.info("存储配额自动清理：删除 %d 个最旧文件", len(removed))
                    except Exception as e:
                        logger.warning("存储配额检查失败: %s", e)
            except Exception as e:
                logger.warning("广播循环异常: %s", e)
            await asyncio_sleep(self.config.get("sample_interval_ms") / 1000.0)


def asyncio_sleep(sec):
    import asyncio

    return asyncio.sleep(sec)


def make_app(engine, storage, config, alerting):
    srv = MonitorServer(engine, storage, config, alerting)
    return srv
