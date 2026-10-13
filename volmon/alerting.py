"""超量监控与告警：阈值 + 持续时长判定，超量片段结束后写入告警表与文本日志。"""
import logging

logger = logging.getLogger("volmon.alert")


class AlertManager:
    def __init__(self, storage, threshold=70.0, sustain_sec=3.0):
        self.storage = storage
        self.threshold = threshold
        self.sustain_sec = sustain_sec
        self.over_start = None      # 超标开始时间（秒，单调）
        self.peak = 0.0            # 本次超标峰值
        self.last_alert = False    # 当前是否处于"持续告警"态
        self.context = {}          # 当前生效策略快照（写入告警记录，便于追溯）

    def update_params(self, threshold, sustain_sec):
        self.threshold = threshold
        self.sustain_sec = sustain_sec

    def set_context(self, ctx):
        """记录"当前生效策略"快照，写入每条超量告警记录。

        ctx 形如 {"period": "白天", "schedule_applied": True,
                  "alarm_enabled": True, "alarm_duration_sec": 10, "alarm_cooldown_sec": 3}
        """
        self.context = dict(ctx or {})

    def check(self, db, now_ms):
        """每个广播周期调用。返回 {'alert': bool, 'peak': float}。"""
        now = now_ms / 1000.0
        if db > self.threshold:
            if self.over_start is None:
                self.over_start = now
                self.peak = db
            else:
                self.peak = max(self.peak, db)
            # 持续超过 sustain_sec 才进入告警态
            self.last_alert = (now - self.over_start) >= self.sustain_sec
        else:
            if self.over_start is not None:
                # 超标片段结束 -> 记录
                dur = now - self.over_start
                self.storage.add_alert(
                    int(self.over_start * 1000),
                    int(now * 1000),
                    round(self.peak, 2),
                    self.threshold,
                    context=self.context,
                )
                ctx = self.context or {}
                logger.warning(
                    "超量片段结束 | 峰值=%.1f dB | 阈值=%.1f | 持续=%.1f s | 策略=%s%s | 声音报警=%s%s",
                    self.peak,
                    self.threshold,
                    dur,
                    ctx.get("period") or "全局",
                    "（分时段）" if ctx.get("schedule_applied") else "",
                    "开" if ctx.get("alarm_enabled") else "关",
                    f" {ctx.get('alarm_duration_sec')}s" if ctx.get("alarm_enabled") else "",
                )
                self.over_start = None
                self.peak = 0.0
                self.last_alert = False
        return {"alert": self.last_alert, "peak": self.peak}
