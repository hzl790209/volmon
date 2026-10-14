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

from .audio import resolve_device_id, list_matching_indices, _hostapi_name_of

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

    设备歧义与采样率兼容（关键修复）：
      Windows 下同一物理设备会被 MME/DirectSound/WASAPI/WDM-KS 各列一份、基础名相同。
      resolve_device_id 优先选 WASAPI 变体，但部分设备的 WASAPI/WDM-KS 端点**不支持
      44100Hz**（报错 "Invalid sample rate"）；而本函数默认按 44100 生成报警音——直接播放
      会失败，进而回退到系统默认（电脑扬声器），表现为"选了 USB 耳机却从电脑出声"。
      故此处：
        1) 候选顺序 = 解析首选索引 -> 同设备的全部 host API 变体（优先最兼容的
           MME/DirectSound）-> 系统默认；尽量尊重用户在面板里选的设备，不轻易退回电脑扬声器。
        2) 每个候选先以**设备默认采样率**尝试，再以 44100 回退——WASAPI 用其 48000 即可成功。
    """
    import numpy as np
    import sounddevice as sd

    def _build_wave(sr, total):
        total = max(0.1, float(total))
        n = int(total * sr)
        t = np.arange(n) / sr
        period = beep_on + beep_off
        env = ((t % period) < beep_on).astype(float)  # 周期性开关，形成嘀嘀声
        return 0.25 * np.sin(2 * np.pi * freq * t) * env

    # host API 兼容性排序：MME/DirectSound 对 44100/48000 均兼容，最稳；其余其后
    _HA_ORDER = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2, "Windows WDM-KS": 3}

    def player(duration_sec):
        # 用户选择的输出设备名（空串/None = 系统默认）
        requested = None
        if device_getter:
            try:
                requested = device_getter() or None
            except Exception as e:
                logger.debug("读取报警输出设备失败: %s", e)
                requested = None

        # 构造候选设备列表（索引）：首选 + 同设备的其它 host API 变体，按兼容性排序
        cands = []
        if requested:
            try:
                idx = resolve_device_id(requested, kind="output")
            except Exception:
                idx = None
            if idx is not None:
                sibs = []
                try:
                    sibs = list_matching_indices(requested, kind="output")
                except Exception:
                    pass
                ranked = sorted(set([idx] + sibs),
                                key=lambda i: _HA_ORDER.get(_hostapi_name_of(i), 9))
                cands.extend(ranked)

        # 系统默认放最后兜底（仍为电脑扬声器，但只在确实所有指定变体都失败时才用）
        cands.append(None)

        last_err = None
        for c in cands:
            # 每个候选先试设备默认采样率，再回退 44100；
            # 部分设备（HECATE/WASAPI）默认 48000，必须用其默认率才能成功。
            srs = []
            if c is not None:
                try:
                    srs.append(int(round(sd.query_devices(c).get("default_samplerate", 44100))))
                except Exception:
                    pass
            if 44100 not in srs:
                srs.append(44100)
            for sr in srs:
                try:
                    sd.play(_build_wave(sr, duration_sec), samplerate=sr,
                            blocking=True, device=c)
                    logger.info("报警声已播放（输出设备=%s, sr=%d）",
                                c if c is not None else "系统默认", sr)
                    return
                except Exception as e:
                    last_err = e
                    logger.warning("报警声在输出设备 #%s (sr=%d) 播放失败，尝试下一个候选: %s",
                                  c, sr, e)
        logger.warning("报警声播放失败（所有候选设备均不可用）: %s", last_err)

    return player
