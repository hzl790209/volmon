"""音量报警管理。

设计要点（用户需求）：
  - 当监测到音量持续超过阈值，由电脑扬声器播放一段报警声（默认约 10 秒）。
  - 报警声会被同一台电脑的麦克风拾回，若不处理会被当成噪音记录、甚至自激重复触发。
  - 因此：报警**播放期间 + 结束后一段冷却期（默认 3 秒）**内，噪声监测整体静默——
    不写入采样、不触发/不延长噪声超量告警；冷却结束后才恢复检测。
  - 报警声播放与监测循环解耦：播放在独立守护线程进行，不阻塞采集广播。

播放器通过依赖注入传入（make_sounddevice_player），便于在测试/无音频环境用空操作替换。
"""

import logging
import threading
import time

logger = logging.getLogger("volmon.alarm")


class AlarmManager:
    """管理"报警触发 → 播放 → 冷却 → 恢复"的状态机。"""

    def __init__(self, enabled=False, duration_sec=10.0, cooldown_sec=3.0,
                 player=None, output_device=None):
        self.enabled = enabled
        self.duration_sec = duration_sec
        self.cooldown_sec = cooldown_sec
        self._player = player  # callable(duration_sec)，在独立线程中执行
        self.output_device = output_device or ""  # 报警声输出设备名；空=系统默认
        self._trigger_ts = 0.0  # 最近一次报警触发时刻（秒，单调时钟）
        self.quiet_until_ms = 0.0   # 报警静音截止时刻（epoch ms），0=未静音
        self._quiet_logged_at = 0.0  # 静音拦截日志节流
        self._lock = threading.Lock()

    def sync_from_config(self, cfg):
        """每轮循环调用，使运行时配置（Web/CLI 修改）即时生效。"""
        self.enabled = bool(cfg.get("alarm_enabled"))
        try:
            self.duration_sec = float(cfg.get("alarm_duration_sec"))
        except (TypeError, ValueError):
            pass
        try:
            self.cooldown_sec = float(cfg.get("alarm_cooldown_sec"))
        except (TypeError, ValueError):
            pass
        try:
            self.quiet_until_ms = float(cfg.get("quiet_until_ms") or 0.0)
        except (TypeError, ValueError):
            pass
        # 报警声输出设备（空串=系统默认输出）
        self.output_device = cfg.get("alarm_output_device") or ""

    def set_player(self, player):
        """注入/替换报警声播放器 callable(duration_sec)。

        之所以不在构造时固定，是因为播放器需要**每次发声时**读取当前输出设备
        （网页改了输出设备要立即生效），故先建 manager 再注入闭包。
        """
        self._player = player

    # ---------- 报警静音时段（人在外面走动时不响，但监测与记录照常） ----------
    def is_quiet(self, now_ms):
        """当前是否处于「报警静音时段」：只压制发声，不影响采样与超量记录。"""
        return self.quiet_until_ms > 0 and float(now_ms) < self.quiet_until_ms

    def quiet_remaining_sec(self, now_ms):
        """距离静音结束还剩多少秒；未静音返回 0。"""
        if not self.is_quiet(now_ms):
            return 0.0
        return max(0.0, (self.quiet_until_ms - float(now_ms)) / 1000.0)

    def trigger(self, now_ms):
        """尝试触发一次报警。仅在启用且当前不处于报警/冷却中时生效。

        返回 True 表示本次实际启动了报警声线程。
        """
        with self._lock:
            if not self.enabled:
                return False
            # 「报警静音时段」内只压制发声，采样与超量记录照常（server 侧未静默）
            if self.is_quiet(now_ms):
                # 触发点每轮都会调用，日志按 30s 节流，避免刷屏
                if now_ms - self._quiet_logged_at > 30000:
                    self._quiet_logged_at = now_ms
                    logger.info(
                        "处于报警静音时段（剩余 %.0f 秒），本次超阈值不发声",
                        self.quiet_remaining_sec(now_ms),
                    )
                return False
            # 已在播放或冷却期内则忽略，避免叠加
            if self._trigger_ts:
                t = now_ms / 1000.0
                if t < self._trigger_ts + self.duration_sec + self.cooldown_sec:
                    return False
            self._trigger_ts = now_ms / 1000.0
        if self._player:
            try:
                threading.Thread(target=self._run_player, daemon=True).start()
            except Exception as e:  # 线程起不来不应拖垮监测循环
                logger.warning("启动报警声线程失败: %s", e)
        logger.info("音量报警触发（持续 %.0fs，冷却 %.0fs）", self.duration_sec, self.cooldown_sec)
        return True

    def test_play(self, seconds=2.0):
        """试听：只播放报警声，不改变监测状态（不进入静默窗口）。

        用于用户确认"报警音是否能听到"，与阈值触发解耦。
        """
        if not self._player:
            logger.warning("无可用报警声播放器（--fake 模式或无音频输出设备）")
            return False
        try:
            threading.Thread(target=self._run_with, args=(seconds,), daemon=True).start()
        except Exception as e:
            logger.warning("试听播放失败: %s", e)
            return False
        return True

    def _run_player(self):
        self._run_with(self.duration_sec)

    def _run_with(self, secs):
        """播放报警声（仅发声，不登记任何"发声窗口"）。

        报警声会从我机扬声器播出、被同一台麦克风拾回——但录音模块已不再依赖
        "跳过这段音频"来规避，而是在报警触发瞬间由 server 调用
        `Recorder.cut_at_trigger()` 直接把录音定格在触发点（详见 recorder 模块文档），
        因此这里不需要再记录发声窗口。
        """
        try:
            self._player(secs)
        except Exception as e:
            logger.warning("报警声播放异常: %s", e)

    def phase(self, now_ms):
        """返回当前阶段：'idle' | 'playing' | 'cooldown'。

        'playing'  = 报警声正在播放（应静默监测）
        'cooldown' = 报警已结束，仍在冷却（应继续静默，避免尾音被记录）
        'idle'     = 正常监测中
        过期状态会在此自愈清理，便于下次触发。
        """
        if not self.enabled:
            return "idle"
        with self._lock:
            base = self._trigger_ts
            if not base:
                return "idle"
            t = now_ms / 1000.0
            if t < base + self.duration_sec:
                return "playing"
            if t < base + self.duration_sec + self.cooldown_sec:
                return "cooldown"
            self._trigger_ts = 0.0  # 清理，恢复可触发
            return "idle"

    def is_suppressed(self, now_ms):
        """报警播放或冷却期间返回 True（此期间不记录、不触发噪声预警）。"""
        return self.phase(now_ms) in ("playing", "cooldown")


def make_sounddevice_player(freq=880.0, beep_on=0.3, beep_off=0.2, device_getter=None):
    """返回一个 callable(duration_sec)：用 sounddevice 在扬声器播放"嘀—嘀—"报警音。

    device_getter: 可选 callable() -> 输出设备名或 None。在**每次播放时**调用，
      因此网页上改了输出设备后立即生效，无需重启服务。返回空串/None 表示用系统默认输出。
    无任何音频输出设备/权限时（如沙箱）会记录警告而不崩溃。
    """
    import numpy as np
    import sounddevice as sd

    def player(duration_sec):
        sr = 44100
        total = max(0.1, float(duration_sec))
        n = int(total * sr)
        t = np.arange(n) / sr
        period = beep_on + beep_off
        env = ((t % period) < beep_on).astype(float)  # 周期性开关，形成嘀嘀声
        wave = 0.25 * np.sin(2 * np.pi * freq * t) * env

        dev = None
        if device_getter:
            try:
                dev = device_getter() or None
            except Exception as e:
                logger.debug("读取报警输出设备失败: %s", e)
                dev = None

        try:
            sd.play(wave, samplerate=sr, blocking=True, device=dev)
            logger.info("报警声已播放（输出设备=%s）", dev or "系统默认")
        except Exception as e:
            logger.warning("报警声播放失败（输出设备 %s 不可用？）: %s", dev or "系统默认", e)

    return player
