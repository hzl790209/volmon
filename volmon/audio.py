"""音频采集引擎：从麦克风读取音频流，计算 RMS -> dBFS，维护滚动缓冲用于校准。"""
import collections
import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger("volmon.audio")

# dBFS 下限钳制，避免静音时 log10(0) = -inf
DBFS_FLOOR = -100.0


class AudioEngine:
    def __init__(self, device="default", sample_rate=44100, blocksize=2048, buffer_len=200):
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = None if device in (None, "default") else device
        self._lock = threading.Lock()
        self._latest_raw = DBFS_FLOOR
        self._buf = collections.deque(maxlen=buffer_len)
        self._stream = None
        self._sink = None           # 原始音频帧出口（录音器用），在 PortAudio 线程里被调用

    def set_frame_sink(self, fn):
        """注册原始音频帧回调 fn(ndarray[frames, ch])。

        注意：fn 运行在 **PortAudio 回调线程**里，必须极快、不能做 I/O 或加锁等耗时操作，
        否则会造成音频断续（underflow）。录音器内部只是往环形缓冲 memcpy，符合要求。
        """
        self._sink = fn

    @staticmethod
    def list_input_devices():
        """返回可用输入设备列表 [(index, name, max_input_channels), ...]"""
        devs = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    devs.append((i, d["name"], int(d["max_input_channels"])))
        except Exception as e:
            logger.warning("列举输入设备失败: %s", e)
        return devs

    @staticmethod
    def list_output_devices():
        """返回可用输出（播放）设备列表 [(index, name, max_output_channels), ...]。

        用于报警声选择从哪路输出：3.5mm 耳机 / USB 耳机 / 本机扬声器等。
        """
        devs = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) > 0:
                    devs.append((i, d["name"], int(d["max_output_channels"])))
        except Exception as e:
            logger.warning("列举输出设备失败: %s", e)
        return devs

    @staticmethod
    def rescan_devices():
        """强制 PortAudio 重新枚举设备——**热插拔后无需重启服务**。

        PortAudio 在进程内只在初始化时枚举一次音频设备，插入 USB 声卡/耳机后
        若不做这一步，`query_devices()` 仍返回旧列表（表现为"新设备不出现"）。
        """
        try:
            sd._terminate()
        except Exception as e:
            logger.debug("PortAudio terminate: %s", e)
        try:
            sd._initialize()
        except Exception as e:
            logger.debug("PortAudio initialize: %s", e)

    def active_device_info(self):
        """返回采集流**实际**在用的设备信息。

        用于回答"到底在用哪个麦克风"——配置里选的设备与流真正打开的设备可能不一致
        （例如改了配置但没重启服务，或指定设备被拔出后回退到默认设备）。
        """
        info = {
            "configured": self.device or "default",
            "index": None,
            "name": None,
            "samplerate": None,
            "running": self._stream is not None,
        }
        try:
            if self._stream is not None:
                d = self._stream.device
                # 单方向流返回 int；双方向返回 (in, out)
                idx = d[0] if isinstance(d, (tuple, list)) else d
                info["index"] = idx if isinstance(idx, int) else None
                try:
                    info["name"] = sd.query_devices(idx)["name"]
                except Exception:
                    pass
                info["samplerate"] = getattr(self._stream, "samplerate", None)
        except Exception as e:
            logger.debug("读取采集设备信息失败: %s", e)
        return info

    def switch_device(self, device="default"):
        """热切换输入设备（**无需重启服务**）。失败时自动回滚到原设备。

        背景：InputStream 在服务启动时打开，单纯改配置不会生效，
        用户会以为"选了 USB 耳机但拾音还是内置麦克风"。
        """
        old = self.device
        was_running = self._stream is not None
        if was_running:
            self.stop()
        self.device = None if device in (None, "", "default") else device
        try:
            if was_running:
                self.start()
        except Exception as e:
            logger.warning("切换输入设备到 %r 失败，回滚到 %r: %s", device, old or "default", e)
            self.device = old
            try:
                if was_running:
                    self.start()
            except Exception as e2:
                logger.warning("回滚后重启采集流失败: %s", e2)
            raise
        logger.info("输入设备已切换：%s -> %s", old or "default", self.device or "default")
        return self.active_device_info()

    def rescan(self):
        """停止采集 → 重初始化 PortAudio → 重新枚举 → 恢复采集。返回最新输入设备列表。"""
        was_running = self._stream is not None
        if was_running:
            self.stop()
        AudioEngine.rescan_devices()
        if was_running:
            try:
                self.start()
            except Exception as e:
                logger.warning("重新扫描后恢复采集失败: %s", e)
        return self.list_input_devices()

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.warning("音频流状态: %s", status)
        try:
            x = indata[:, 0] if indata.ndim > 1 else indata
            x = np.asarray(x, dtype=np.float32)
            if x.size == 0:
                return
            rms = float(np.sqrt(np.mean(x ** 2)))
            # 必须转成 Python 原生 float：np.float64 参与比较会产出 np.bool_，
            # 而 json 无法序列化 np.bool_（会导致 WebSocket 广播异常）
            db = float(20.0 * np.log10(rms)) if rms > 0 else DBFS_FLOOR
            db = float(max(db, DBFS_FLOOR))
            with self._lock:
                self._latest_raw = db
                self._buf.append(db)
            # 原始帧交给录音器（先算完 dB 再做，且必须 copy：indata 是 PortAudio 的复用缓冲）
            if self._sink is not None:
                try:
                    self._sink(indata.copy())
                except Exception as e:
                    logger.debug("音频帧出口异常: %s", e)
        except Exception as e:
            logger.debug("回调计算异常: %s", e)

    def start(self):
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=self.sample_rate,
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()
        logger.info("音频采集已启动 (device=%s, sr=%d)", self.device or "default", self.sample_rate)

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning("停止音频流异常: %s", e)
            self._stream = None

    def is_silent(self):
        """麦克风是否持续输出"数字静音"。

        常见于：macOS 未授予麦克风权限（流能打开但被静音成全零）、系统输入被静音、
        或选错了输入设备。此时读数会恒为 DBFS_FLOOR，波形呈一条直线，容易被误判为"采集停止"。
        """
        with self._lock:
            vals = list(self._buf)[-50:]
        if not vals:
            return True  # 回调从未产出数据，同样视为无有效输入
        return all(v <= DBFS_FLOOR + 1e-6 for v in vals)

    def raw_db(self):
        with self._lock:
            return self._latest_raw

    def recent_raw_avg(self, n=100):
        with self._lock:
            vals = list(self._buf)[-n:]
        if not vals:
            return DBFS_FLOOR
        return float(np.mean(vals))
