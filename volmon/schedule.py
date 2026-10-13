"""分时段策略：白天 / 晚间两套阈值与报警策略。

设计要点（对应用户需求"白天和晚间 2 个时间段用不同管理策略"）：
  - 两个时段**各自可设起止时刻**（HH:MM），支持跨零点（如晚间 22:00 → 07:00）。
  - `schedule_enabled=False` 时完全沿用全局配置，**与旧行为一致**（向后兼容，不会突然改变现有阈值）。
  - 开启后按时段覆盖：阈值 / 持续时长 / 报警开关 / 报警时长 / 冷却时长。
  - **不写回配置**：只在运行时计算"有效值"，避免把时段值污染成全局配置，
    也让"关掉分时段"能干净地回到全局设置。
  - 两个时段若未覆盖满 24 小时（有空档或重叠），`validate()` 会给出提示；
    判定顺序为「先白天后晚间」，空档时段回退为白天，保证任何时刻都有确定策略。
"""
import logging
import time

logger = logging.getLogger("volmon.schedule")

DAY = "day"
NIGHT = "night"

PERIOD_LABEL = {DAY: "白天", NIGHT: "晚间"}

# 每个时段对应的配置键
KEYS = {
    DAY: {
        "start": "day_start",
        "end": "day_end",
        "threshold_db": "day_threshold_db",
        "sustain_sec": "day_sustain_sec",
        "alarm_enabled": "day_alarm_enabled",
        "alarm_duration_sec": "day_alarm_duration_sec",
        "alarm_cooldown_sec": "day_alarm_cooldown_sec",
    },
    NIGHT: {
        "start": "night_start",
        "end": "night_end",
        "threshold_db": "night_threshold_db",
        "sustain_sec": "night_sustain_sec",
        "alarm_enabled": "night_alarm_enabled",
        "alarm_duration_sec": "night_alarm_duration_sec",
        "alarm_cooldown_sec": "night_alarm_cooldown_sec",
    },
}

SCHEDULE_KEYS = [
    "schedule_enabled",
    "day_start", "day_end", "night_start", "night_end",
    "day_threshold_db", "night_threshold_db",
    "day_sustain_sec", "night_sustain_sec",
    "day_alarm_enabled", "night_alarm_enabled",
    "day_alarm_duration_sec", "night_alarm_duration_sec",
    "day_alarm_cooldown_sec", "night_alarm_cooldown_sec",
]


def parse_hm(s, default=0):
    """把 "HH:MM" 解析为自零点起的分钟数；解析失败返回 default。"""
    try:
        s = str(s or "").strip()
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


def now_minutes(now=None):
    """当前时刻自零点起的分钟数。now 为 epoch 秒。"""
    t = time.localtime(now if now is not None else time.time())
    return t.tm_hour * 60 + t.tm_min


def in_window(minutes, start, end):
    """判断分钟数是否落在 [start, end) 窗口内；支持跨零点。

    start == end 视为覆盖全天。
    """
    if start == end:
        return True
    if start < end:
        return start <= minutes < end
    # 跨零点：如 22:00 -> 07:00
    return minutes >= start or minutes < end


def active_period(cfg, now=None):
    """返回当前应生效的时段：'day' | 'night'。"""
    m = now_minutes(now)
    if in_window(m, parse_hm(cfg.get("day_start"), 7 * 60),
                 parse_hm(cfg.get("day_end"), 22 * 60)):
        return DAY
    if in_window(m, parse_hm(cfg.get("night_start"), 22 * 60),
                 parse_hm(cfg.get("night_end"), 7 * 60)):
        return NIGHT
    return DAY  # 存在空档时回退白天，保证任何时刻都有确定策略


def resolve(cfg, now=None):
    """计算当前**有效配置**。

    返回 {"period", "period_label", "applied", "config"}：
      - config 为原始配置的副本（开启分时段时按时段覆盖若干键）
      - applied=False 表示未启用分时段，config 与全局配置完全一致
    """
    base = cfg.get_all() if hasattr(cfg, "get_all") else dict(cfg)
    period = active_period(base, now)
    if not base.get("schedule_enabled"):
        return {
            "period": period,
            "period_label": PERIOD_LABEL[period],
            "applied": False,
            "config": base,
        }

    k = KEYS[period]
    out = dict(base)

    def pick(key, cast, fallback_key):
        v = base.get(key, None)
        if v is None:
            v = base.get(fallback_key)
        try:
            return cast(v)
        except (TypeError, ValueError):
            return cast(base.get(fallback_key))

    out["threshold_db"] = pick(k["threshold_db"], float, "threshold_db")
    out["sustain_sec"] = pick(k["sustain_sec"], float, "sustain_sec")
    out["alarm_enabled"] = bool(base.get(k["alarm_enabled"], base.get("alarm_enabled")))
    out["alarm_duration_sec"] = pick(k["alarm_duration_sec"], float, "alarm_duration_sec")
    out["alarm_cooldown_sec"] = pick(k["alarm_cooldown_sec"], float, "alarm_cooldown_sec")
    return {
        "period": period,
        "period_label": PERIOD_LABEL[period],
        "applied": True,
        "config": out,
    }


def next_switch_minutes(cfg, now=None):
    """距离切换到另一个时段还剩多少分钟。"""
    period = active_period(cfg, now)
    end = parse_hm(cfg.get(KEYS[period]["end"]))
    delta = (end - now_minutes(now)) % 1440
    return delta if delta > 0 else 1440


def validate(cfg):
    """检查两个时段是否覆盖满 24 小时，返回提示列表（空列表=正常）。

    逐分钟检查（1440 次），只在查询状态时调用，不在采集热循环里跑。
    """
    d0, d1 = parse_hm(cfg.get("day_start"), 7 * 60), parse_hm(cfg.get("day_end"), 22 * 60)
    n0, n1 = parse_hm(cfg.get("night_start"), 22 * 60), parse_hm(cfg.get("night_end"), 7 * 60)
    gap = overlap = 0
    for m in range(1440):
        a = in_window(m, d0, d1)
        b = in_window(m, n0, n1)
        if a and b:
            overlap += 1
        elif not a and not b:
            gap += 1
    tips = []
    if gap:
        tips.append(f"两个时间段未覆盖全天，有 {gap} 分钟空档（将按白天策略执行）")
    if overlap:
        tips.append(f"两个时间段有 {overlap} 分钟重叠（重叠时按白天优先）")
    return tips


def status(cfg, now=None):
    """给接口/页面用的一览状态。"""
    st = resolve(cfg, now)
    base = cfg.get_all() if hasattr(cfg, "get_all") else dict(cfg)
    eff = st["config"]
    return {
        "enabled": bool(base.get("schedule_enabled")),
        "period": st["period"],
        "period_label": st["period_label"],
        "applied": st["applied"],
        "threshold_db": float(eff["threshold_db"]),
        "sustain_sec": float(eff["sustain_sec"]),
        "alarm_enabled": bool(eff["alarm_enabled"]),
        "alarm_duration_sec": float(eff["alarm_duration_sec"]),
        "alarm_cooldown_sec": float(eff["alarm_cooldown_sec"]),
        "next_switch_min": next_switch_minutes(base, now),
        "windows": {
            DAY: {"start": base.get("day_start"), "end": base.get("day_end")},
            NIGHT: {"start": base.get("night_start"), "end": base.get("night_end")},
        },
        "warnings": validate(base),
    }
