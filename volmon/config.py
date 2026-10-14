"""配置管理：读取/写入 data/config.json，并提供默认值。"""
import json
import os
import threading

DEFAULTS = {
    "threshold_db": 70.0,        # 预警阈值（校准后 dB）
    "sustain_sec": 3.0,          # 持续超过多久才触发告警（秒）
    "sample_interval_ms": 100,   # 实时计算/广播间隔
    "log_interval_ms": 1000,     # 采样落库频率（后台详细日志采样率）
    "calib_offset_db": 0.0,      # 校准偏移兜底值（实测值 - 软件"基准估算值"）。
                                 # 正常按**每台麦克风分别**存在 device_calib 里；只有当当前设备
                                 # 没有任何专属校准记录时，才退回用这个全局值（兼容旧配置）。
    "device_calib": {},          # 校准偏移表：{物理设备组键: 偏移 dB}。
                                 # 组键由 volmon.audio.device_group_key 算出（已跨 host API 归并，
                                 # 如 "麦克风 (Realtek(R) Audio)" 与 "...Mic input" 同一键），
                                 # 因此换 USB 耳机 / 拔插 3.5mm 耳麦后各自用各自的偏移，不会串味。
                                 # 未出现在表里的设备 => 未校准，按 ref_spl_offset_db 估算。
    "meter_smooth_sec": 0.5,     # 读数平滑窗口（秒）：默认情况下不用单个音频块的瞬时值，
                                 # 而是取最近该窗口内的**能量 RMS**（近似声级计 Fast 档）。
                                 # 好处：读数不抖、且与校准时用的统计量同口径。
                                 # 设 0 则退回"每个音频块瞬时值"（波动大时会明显偏高于平均感觉）。
    "ref_spl_offset_db": 90.0,   # dBFS→dB SPL 参考偏移：dB SPL ≈ dBFS + 该值。
                                 # 未校准时用它把负数 dBFS 换算成接近真实的声压级估算值；
                                 # 用专业设备校准后由 calib_offset_db 进一步修正误差。
    "device": "default",         # 输入设备名或 "default"
    "alarm_enabled": False,      # 超阈值后是否由电脑播放音量报警声
    "alarm_duration_sec": 10.0,  # 单次报警声持续时长（秒）
    "alarm_cooldown_sec": 5.0,   # 报警结束后冷却时长（秒），期间仍不记录/不触发噪声预警
    "alarm_output_device": "",   # 报警声输出设备名；空 = 系统默认输出设备。
                                 # 可选 3.5mm 耳机 / USB 耳机 / 本机扬声器等（见 /api/output-devices）
    "device_aliases": {},        # 设备友好别名：{设备基础名: 展示名}，让"麦克风 (Realtek(R) Audio)"
                                 # 这类系统名在面板里显示为"3.5mm耳麦"之类好认的名字
    "show_all_devices": False,   # 设备下拉是否列出全部（含系统底层端点/回环混音/重复通路）。
                                 # 默认 False：同一物理设备的多个 host API 通路合并成一项，
                                 # 并剔除虚拟映射器、立体声混音、WDM-KS 底层端点等日常用不到的项。
    "quiet_until_ms": 0.0,       # 报警静音截止时刻（epoch 毫秒；0 = 未静音）。
                                 # 用于"人要走开一段时间"的场景：期间监测与超量记录照常，
                                 # 但即使超阈值也**不播放报警声**。
    # ---- 分时段策略：白天 / 晚间两套阈值与报警策略 ----
    "schedule_enabled": False,   # 总开关；关闭时完全沿用上面的全局阈值/报警设置（向后兼容）
    "day_start": "07:00", "day_end": "22:00",
    "night_start": "22:00", "night_end": "07:00",   # 支持跨零点
    "day_threshold_db": 70.0,   "night_threshold_db": 55.0,
    "day_sustain_sec": 3.0,     "night_sustain_sec": 3.0,
    "day_alarm_enabled": False, "night_alarm_enabled": False,
    "day_alarm_duration_sec": 10.0, "night_alarm_duration_sec": 10.0,
    "day_alarm_cooldown_sec": 5.0,  "night_alarm_cooldown_sec": 5.0,
    "db_path": "data/volmon.db",
    "log_path": "data/volmon.log",
    "alerts_csv_path": "data/alerts.csv",  # 超量告警本地 CSV 落盘（Excel 可直接打开）
    "log_rotate_daily": True,   # 运行日志 / 告警 CSV 按日切割：当前文件只装今天，
                                # 跨日自动归档为 volmon_YYYY-MM-DD.log / alerts_YYYY-MM-DD.csv。
                                # 采样数据不切割，仍在 data/volmon.db 单库内（用「日志维护」清理）。
    "log_keep_days": 30,        # 归档文件保留天数（0 = 永不删除）
    # ---- 每日报警日志邮件 ----
    "mail_enabled": False,      # 是否每天自动把报警日志发到邮箱
    "mail_host": "smtp.qq.com", # SMTP 服务器（QQ: smtp.qq.com / 163: smtp.163.com / Gmail: smtp.gmail.com）
    "mail_port": 465,           # 端口（465=SSL，587=STARTTLS）
    "mail_ssl": True,           # True=SMTP_SSL（465）；False 时若 mail_starttls=True 则走 STARTTLS
    "mail_starttls": False,     # 端口 587 时改为 True
    "mail_user": "",            # 发件邮箱账号（如 123456@qq.com）
    "mail_password": "",        # **授权码**（不是登录密码）：QQ/163 需在邮箱设置里开启 SMTP 并生成
    "mail_from": "",            # 发件人显示地址；留空则用 mail_user
    "mail_to": "",              # 收件人；多个用英文逗号或分号分隔
    "mail_send_time": "09:00",  # 每天几点发送（24 小时制 HH:MM）
    "mail_day_offset": 1,       # 报表范围：1=昨天全天（默认，数据完整）；0=今天 00:00 至今
    "mail_attach_csv": True,    # 是否附带当天告警 CSV 附件
    "mail_skip_if_empty": False,  # 当天无超量告警时是否跳过发送（True=不发"平安信"）
    "mail_timeout_sec": 30,     # SMTP 连接超时（秒）
    "mail_last_sent_day": "",   # 内部：最近一次成功发送的"发送日"，避免同一天重复发
    "mail_last_result": "",     # 内部：最近一次发送结果（页面展示）
    # ---- 触发录音：把报警时的真实声音存下来 ----
    "rec_enabled": False,       # 总开关（默认关闭：涉及录音，请自行确认后再开）
    "rec_trigger": "alert",     # 触发源：alert=正式报警（推荐）/ over=刚开始超阈值 / off
    "rec_pre_sec": 5.0,         # 触发**前**保留多少秒（由常驻环形缓冲提供，不丢开头）
    "rec_post_sec": 5.0,        # 触发**后**继续录多少秒
    "rec_dir": "data/recordings",  # 录音目录（内部按日分子目录）
    "rec_keep_days": 30,        # 录音保留天数（0 = 不按天删）
    "rec_max_files": 500,       # 最多保留多少个文件（超出删最旧；0 = 不限）
    "rec_max_mb": 2048,         # 录音总大小上限 MB（超出删最旧；0 = 不限）
    "rec_gain_mode": "auto",    # 写盘增益模式：auto=自动峰值归一化（默认，把最响的一刻顶到接近满刻度，
                                #        回放最响亮且不会削波失真；适合"录一声响"的触发录音场景）
                                #   fixed=按 rec_gain_db 固定放大/衰减（过大易削波失真）
                                #   off=原始录音（不改音量，听感最真实，但弱信号回放极小声）
    "rec_gain_db": 0.0,         # fixed 模式下的增益 dB（可正可负；0 = 不改音量）
    "rec_cut_on_alarm": True,   # 开启声音报警时，报警一触发就把正在进行的录音**截断在触发瞬间**
                                # （仅保留"触发前"那段真实环境音，绝不录本机扬声器放出去的报警声）。
                                # 报警声从触发点才响起，触发点之后必然混有报警声；与其靠计时去
                                # "躲"那段报警声（仍会把尾音漏进后置段），不如直接在触发点一刀切：
                                # 前置段照留，触发点之后的所有音频一律不录，WAV 里绝无报警声。
    "host": "0.0.0.0",
    "port": 8000,
    # 设备友好别名：把难认的系统设备名映射成易认的名字（如 3.5mm 麦 -> "3.5mm耳麦"）。
    # 键为设备基础名（不含 host API 后缀），值为展示名；留空字典=不接管任何设备名。
    "device_aliases": {},
}


class Config:
    def __init__(self, path="data/config.json"):
        self.path = path
        self.data = dict(DEFAULTS)
        self.lock = threading.Lock()
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_all(self):
        return dict(self.data)

    def set(self, key, value):
        with self.lock:
            # 尝试按默认值类型转换
            if key in DEFAULTS:
                t = type(DEFAULTS[key])
                try:
                    # 布尔值特殊处理：bool("false") 会得到 True，需要先按字符串语义转换
                    if t is bool and isinstance(value, str):
                        value = value.strip().lower() in ("1", "true", "yes", "on", "y")
                    else:
                        value = t(value)
                except Exception:
                    pass
            self.data[key] = value
            self.save()

    def set_many(self, mapping):
        for k, v in mapping.items():
            self.set(k, v)

    def reset(self):
        """恢复全部配置为出厂默认值（不含运行时路径之外的自定义）。"""
        with self.lock:
            self.data = dict(DEFAULTS)
            self.save()
        return self.get_all()
