"""模拟音频引擎：在无麦克风环境（演示 / 沙箱 / 测试）下生成逼真的音量变化。

实现与 AudioEngine 相同的接口（raw_db / recent_raw_avg / list_input_devices），
因此可直接替换真实引擎，用于：
  - 无麦克风时演示软件全功能
  - 自动化测试（L2/L3 前置）
  - 用户想先看效果再接真实麦克风

演示模式下同样会产出**模拟的原始音频帧**（噪声），因此录音功能在 --fake 下也能跑通，
只是录到的是合成噪声而不是真实声音。
"""
import math
import os
import random
import threading
import time

import numpy as np

# 与真实引擎保持一致的音频参数
SR = 44100
BLOCK = 2048


class SimEngine:
    def __init__(self, base=60.0, amp=12.0, period=9.0, noise=2.0):
        self.base = base
        self.amp = amp
        self.period = period
        self.noise = noise
        self._t0 = time.time()
        self._buf = []
        self._burst = 0.0
        self._burst_t = -99.0
        self._sink = None
        self._thread = None
        self._running = False

    def _value(self):
        """生成更接近真实环境噪声的 dB 序列：缓慢漂移 + 中频起伏 + 高频抖动 +
        偶发突发（敲击/喊叫等），而非单一正弦波。"""
        t = time.time() - self._t0
        # 缓慢漂移：环境本底随时间变化
        drift = 0.45 * self.amp * math.sin(2 * math.pi * t / self.period)
        # 中频起伏：人声 / 设备间歇性噪声（非整周期，避免规则波形）
        mid = 0.30 * self.amp * math.sin(2 * math.pi * t / (self.period / 2.3) + 1.1)
        # 高频抖动：随机
        jitter = random.uniform(-0.5, 0.5) * self.amp
        # 偶发突发：指数衰减包络，模拟真实中的瞬时噪声事件
        if random.random() < 0.04:
            self._burst = random.uniform(0.55, 1.0) * self.amp
            self._burst_t = t
        if self._burst and (t - self._burst_t) < 0.5:
            burst = self._burst * math.exp(-(t - self._burst_t) / 0.13)
        else:
            burst = 0.0
            self._burst = 0.0
        return self.base + drift + mid + jitter + burst

    def raw_db(self):
        v = self._value()
        self._buf.append(v)
        if len(self._buf) > 200:
            self._buf.pop(0)
        return round(v, 2)

    def recent_raw_avg(self, n=100):
        if not self._buf:
            return self.base
        return round(sum(self._buf[-n:]) / len(self._buf[-n:]), 2)

    def is_silent(self):
        """模拟引擎始终有数据，不会静音。"""
        return False

    def list_input_devices(self):
        return [(0, "模拟输入（无麦克风/演示用）", 1)]

    def list_output_devices(self):
        return [(0, "模拟输出（演示用，不发声）", 2)]

    def rescan(self):
        """模拟引擎无真实设备可扫描，直接返回固定列表。"""
        return self.list_input_devices()

    def active_device_info(self):
        return {
            "configured": "default", "index": 0,
            "name": "模拟输入（无麦克风/演示用）", "samplerate": 44100, "running": True,
        }

    def switch_device(self, device="default"):
        return self.active_device_info()

    def set_frame_sink(self, fn):
        """与 AudioEngine 接口对齐：注册原始音频帧回调。"""
        self._sink = fn

    def _audio_loop(self):
        """按真实节奏产出模拟音频块（噪声，幅度跟随当前 dB），喂给录音器。"""
        rng = np.random.default_rng(20260927)
        while self._running:
            db = self._value()
            amp = float(10 ** ((db - 90.0) / 20.0))       # dB SPL 估算 -> 线性幅度
            amp = max(0.0, min(amp, 1.0))
            block = (rng.standard_normal((BLOCK, 1)) * amp).astype(np.float32)
            if self._sink is not None:
                try:
                    self._sink(block)
                except Exception:
                    pass
            time.sleep(BLOCK / float(SR))

    def start(self):
        """与 AudioEngine 接口对齐；有 sink 时额外起一个模拟音频线程。"""
        if self._sink is None:
            return None
        if self._running:
            return None
        self._running = True
        self._thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._thread.start()
        return None

    def stop(self):
        self._running = False
        return None
