"""触发录音：把"触发报警的那段真实声音"完整保存下来。

## 为什么不是"报警了才按下录音键"

声音往往很短（敲门、喊叫、器物掉落），而从"声音发生 → 算 dB → 判定超阈值 → 判定持续时长 →
触发报警 → 打开文件准备写入"这一整条链路有延迟，等程序反应过来，声音早过去了。

## 解决办法：常驻环形缓冲 + 前置录音（pre-roll）

采集流一直在跑，最近 N 秒的原始音频**始终留在内存环形缓冲里**（不是文件、不落盘）。
报警触发的瞬间，直接从缓冲里把"触发前 N 秒"**倒出来**，再接着录"触发后 M 秒"，拼成完整一段。

    时间轴 ──────────────────────────────────────────────►
    声音:            ▁▂▅█▇▅▂▁              （0.3 秒）
                     ↑
    缓冲:  [一直在滚动保留最近 5 秒……][触发点][继续录 5 秒……]
            └── pre_roll（内存里取）──┘        └─ post_roll ─┘
    文件:  ┕━━━━━━━━━━━ 完整，开头不丢 ━━━━━━━━━━━┙

这样无论触发链路多慢、声音多短，**开头都不会缺**。

## 其它设计点

- 环形缓冲用 numpy 预分配数组，写入只是 memcpy；一块 2048 帧≈8 KB，开销可忽略
- 只在开关打开时才填充缓冲；内存占用 = 采样率 × 秒数 × 通道 × 4 字节（44.1k×12s×1 ≈ 2 MB）
- 存 16-bit PCM WAV（标准库 `wave`，不新增依赖），按日分目录，复用保留天数与配额清理
- 文件名在**触发那一刻**就确定，随后回填到对应告警记录里（`audio_file` 列），
  页面上可直接播放

## 报警声自激：为什么是"触发瞬间截断"而不是"跳过那段"

开启了**声音报警**后，报警声是从本机扬声器放出去的，会被同一台电脑的麦克风**原样拾回**——
录进 WAV 就是一段刺耳的"嘀嘀"声，把真实环境音盖掉，录音也就白录了。

这是物理串扰，软件层面没法把两个混在一起的声音分开（除非做回声消除 AEC，代价太大）。
所以采取**最干脆也最可靠的办法**：报警声是从触发点才响起来的，那么"触发点之后"的音频
必然混有报警声——既然如此，**报警一触发，立刻把录音定格在触发瞬间**：前置段
（环形缓冲里已取出的那几秒真实环境音）照常保留，触发点之后的所有音频一律不录。

    ── 报警声（在触发点才响起）──────┐
                                      │ 这一段不录
    麦克风:  ▁▂▃▅█▇▅▃▁  ▁▂▃▅█▇▅▃▁   ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁
             ↑触发的真实声音           ↑报警声
    录音:    [前置段保留]  ‖  （触发点一刀切，后面不录）

这样无论声卡延迟、房间混响多大，WAV 里都**不可能**出现报警声——
因为它根本就不在"触发点之前"那段里，而触发点之后我们压根不录。

（历史方案对比：早期用"跳过污染窗口"——报警响的那几秒整段不录、后置段再顺延凑满。
实测仍会把报警声的尾音漏进后置段，故废弃，改为现在的"截断"策略，简单且无懈可击。）
"""
import datetime
import logging
import os
import threading
import time
import wave

import numpy as np

logger = logging.getLogger("volmon.recorder")

MAX_CONCURRENT = 3      # 同时进行的录音任务上限（防止并发报警把内存吃光）


class RingBuffer:
    """定长环形缓冲：持续写入最新音频帧，可随时取"最近 N 秒"。"""

    def __init__(self, samplerate, seconds, channels=1):
        self.samplerate = int(samplerate or 44100)
        self.channels = max(1, int(channels or 1))
        self.capacity = max(1, int(self.samplerate * max(1.0, float(seconds))))
        self.buf = np.zeros((self.capacity, self.channels), dtype=np.float32)
        self.pos = 0
        self.written = 0
        self._lock = threading.Lock()

    def seconds(self):
        return self.capacity / float(self.samplerate)

    def _norm(self, data):
        d = np.asarray(data, dtype=np.float32)
        if d.ndim == 1:
            d = d.reshape(-1, 1)
        if d.shape[1] != self.channels:        # 通道数变了（换设备）时取前 n 路
            d = d[:, : self.channels]
        return d

    def write(self, data):
        d = self._norm(data)
        n = d.shape[0]
        if n <= 0:
            return
        with self._lock:
            if n >= self.capacity:
                self.buf[:] = d[-self.capacity:]
                self.pos = 0
                self.written += n
                return
            end = self.pos + n
            if end <= self.capacity:
                self.buf[self.pos:end] = d
            else:
                first = self.capacity - self.pos
                self.buf[self.pos:] = d[:first]
                self.buf[: n - first] = d[first:]
            self.pos = (self.pos + n) % self.capacity
            self.written += n

    def tail(self, seconds):
        """取最近 seconds 秒（按写入顺序，正序返回副本）。"""
        want = int(float(seconds) * self.samplerate)
        want = max(0, min(want, self.capacity, self.written))
        if want == 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        with self._lock:
            start = (self.pos - want) % self.capacity
            if start + want <= self.capacity:
                return self.buf[start:start + want].copy()
            first = self.capacity - start
            return np.concatenate([self.buf[start:], self.buf[: want - first]], axis=0).copy()


class Recorder:
    """触发录音器：由采集回调持续喂数据，由主循环触发与收尾。"""

    def __init__(self, config):
        self.cfg = config
        self.ring = None
        self.samplerate = 44100
        self.channels = 1
        self.pending = []
        self._lock = threading.Lock()
        self.last_file = None
        self.last_error = ""
        self.saved_count = 0
        self._cut_on_alarm = True   # 报警声触发时，把正在进行的录音截断在触发瞬间（仅留触发前音频）
        self._cut_queue = []        # 已被截断、等待写盘收工的任务（不再接收新音频帧）

    # ---------- 报警声截断 ----------
    def set_cut_on_alarm(self, on):
        """配置层面是否启用"报警触发即截断"。

        由 server 在每轮循环按网页/CLI 配置同步，使开关即时生效，无需重启服务。
        """
        self._cut_on_alarm = bool(on)

    def cut_on_alarm(self):
        """当前是否启用"报警触发即截断"。"""
        return self._cut_on_alarm

    # ---------- 装配 ----------
    def attach(self, samplerate, channels=1):
        """采集流就绪后调用：建立环形缓冲（容量 = 前置秒数 + 2 秒余量，至少 10 秒）。"""
        try:
            pre = float(self.cfg.get("rec_pre_sec") or 5.0)
        except (TypeError, ValueError):
            pre = 5.0
        self.samplerate = int(samplerate or 44100)
        self.channels = max(1, int(channels or 1))
        self.ring = RingBuffer(self.samplerate, max(pre + 2.0, 10.0), self.channels)
        logger.info("录音缓冲已就绪：%.1f 秒 / %d Hz / %d 通道",
                    self.ring.seconds(), self.samplerate, self.channels)
        return self.ring

    @property
    def ready(self):
        return self.ring is not None

    def enabled(self):
        return bool(self.cfg.get("rec_enabled"))

    def base_dir(self):
        return self.cfg.get("rec_dir") or os.path.join("data", "recordings")

    # ---------- 数据流 ----------
    def feed(self, data):
        """采集回调调用：写环形缓冲；若正在录音，同时追加到进行中的任务。

        注意：这里**不做**任何"报警声跳过"处理——报警声从触发点才响起，触发点之后的
        音频由 server 在报警触发时调用 `cut_at_trigger` 直接截断（详见模块文档），
        因此 WAV 里不会出现本机扬声器放出去的报警声。
        """
        if self.ring is None:
            return
        try:
            self.ring.write(data)
        except Exception as e:
            logger.debug("环形缓冲写入异常: %s", e)
            return
        if not self.pending:
            return
        d = np.asarray(data, dtype=np.float32)
        if d.ndim == 1:
            d = d.reshape(-1, 1)
        if d.size == 0:
            return
        with self._lock:
            for p in self.pending:
                # 每个任务各存一份副本（并发录音通常只有 1 个，内存开销可控）
                p["chunks"].append(d.copy())
                p["frames"] += d.shape[0]

    # ---------- 触发 ----------
    def trigger(self, peak_db=None, reason="alert"):
        """报警触发：立即从环形缓冲取出"触发前"的音频，剩下的实时追加。

        返回相对路径（如 "2026-09-27/rec_153012_480.wav"）；未开启/不可用返回 None。
        文件名在触发瞬间就确定，稍后回填到对应告警记录。
        """
        if not self.enabled() or self.ring is None:
            return None
        with self._lock:
            if len(self.pending) >= MAX_CONCURRENT:
                return None
        try:
            pre_sec = float(self.cfg.get("rec_pre_sec") or 5.0)
            post_sec = float(self.cfg.get("rec_post_sec") or 5.0)
        except (TypeError, ValueError):
            pre_sec = post_sec = 5.0

        now = time.time()
        dt = datetime.datetime.fromtimestamp(now)
        day = dt.strftime("%Y-%m-%d")
        name = f"rec_{dt.strftime('%H%M%S')}_{int((now % 1) * 1000):03d}.wav"
        rel = f"{day}/{name}"

        task = {
            "rel": rel,
            "day": day,
            "start": now,
            "pre": self.ring.tail(pre_sec),
            "chunks": [],
            "frames": 0,
            "post_sec": post_sec,
            "peak_db": peak_db,
            "reason": reason,
            "closed": False,
        }
        task["frames"] = task["pre"].shape[0]
        task["pre_frames"] = task["pre"].shape[0]
        with self._lock:
            self.pending.append(task)
        logger.info("开始录音：%s（前 %.1fs + 后 %.1fs，触发源=%s）", rel, pre_sec, post_sec, reason)
        return rel

    # ---------- 报警触发：在触发瞬间截断 ----------
    def cut_at_trigger(self, reason="alarm"):
        """报警声触发时由 server 调用：把正在进行的录音**立即定格在触发瞬间**。

        报警声从本机扬声器播出、被同一台麦克风拾回，录进去就是刺耳的嘀嘀声。
        报警是在触发点才响起来的，因此"触发点之后"的音频必然混有报警声——
        这里直接把录音掐断在触发点：前置段（环形缓冲里已取出的真实环境音）照常保留，
        触发点之后的所有音频一律不录，从根本上杜绝报警声进入 WAV。

        被标记截断的任务立刻移出 `pending`（不再接收新音频帧），转入 `_cut_queue`，
        下一轮 `tick()` 即写盘收工，不再等 post 秒。
        """
        with self._lock:
            if not self.pending:
                return 0
            moved = []
            for p in self.pending:
                if p.get("cut"):
                    continue
                p["cut"] = True
                p["cut_reason"] = reason
                moved.append(p)
            self._cut_queue.extend(moved)
            self.pending = []
        if moved:
            logger.info("报警触发：%d 段进行中的录音已在触发瞬间截断（仅保留触发前音频）", len(moved))
        return len(moved)

    # ---------- 收尾 ----------
    def tick(self):
        """主循环调用：把录够时长的任务写盘。返回完成的 [(rel_path, 绝对path, meta)]。

        判据以**实际收到的音频帧数**为准（够 post 秒就收工），
        真实时间只作兜底（采集流断了也不能一直挂着不写盘）。
        已被报警截断的任务（`_cut_queue`）直接收工，不再等 post 秒。
        """
        if not self.pending and not self._cut_queue:
            return []
        now = time.time()
        finished = []
        with self._lock:
            keep = []
            for p in self.pending:
                # 触发后实际收到的音频帧数（够 post 秒即收工）
                got = p["frames"] - p.get("pre_frames", 0)
                need = int(p["post_sec"] * self.samplerate)
                if got >= need or now - p["start"] >= p["post_sec"] + 5.0:
                    finished.append(p)      # 从 pending 移除后 feed 自然不再追加
                else:
                    keep.append(p)
            self.pending = keep
            # 报警截断任务：直接收工（不追加触发点之后的音频）
            if self._cut_queue:
                finished.extend(self._cut_queue)
                self._cut_queue = []
        out = []
        for p in finished:
            path = self._write_wav(p)
            if path:
                self.saved_count += 1
                self.last_file = p["rel"]
                out.append((p["rel"], path, {
                    "peak_db": p.get("peak_db"),
                    "reason": p.get("reason"),
                    "trigger_ts": int(p["start"] * 1000),
                    "seconds": round(p["frames"] / float(self.samplerate), 2),
                    "cut": bool(p.get("cut")),
                }))
        if out:
            self.prune()
        return out

    def _write_wav(self, p):
        try:
            day_dir = os.path.join(self.base_dir(), p["day"])
            os.makedirs(day_dir, exist_ok=True)
            path = os.path.join(self.base_dir(), p["rel"].replace("/", os.sep))
            parts = [p["pre"]] + [c for c in p["chunks"]]
            parts = [x for x in parts if x is not None and x.size]
            if not parts:
                return None
            data = np.concatenate(parts, axis=0)
            if data.size == 0:
                return None
            # float32 [-1,1] -> int16 PCM
            pcm = np.clip(data, -1.0, 1.0)
            ints = (pcm * 32767.0).astype("<i2")
            with wave.open(path, "wb") as w:
                w.setnchannels(self.channels)
                w.setsampwidth(2)
                w.setframerate(self.samplerate)
                w.writeframes(ints.tobytes())
            secs = data.shape[0] / float(self.samplerate)
            if p.get("cut"):
                logger.info("录音已保存（报警触发·已截断）：%s（%.1f 秒，仅含触发前音频）",
                            p["rel"], secs)
            else:
                logger.info("录音已保存：%s（%.1f 秒）", p["rel"], secs)
            self.last_error = ""
            return path
        except Exception as e:
            self.last_error = f"写录音失败: {e}"
            logger.warning("写录音文件失败: %s", e)
            return None

    # ---------- 清理与统计 ----------
    def prune(self):
        """按保留天数删除整日目录；再按文件数与总大小配额删最旧的。"""
        base = self.base_dir()
        if not os.path.isdir(base):
            return []
        try:
            keep_days = int(self.cfg.get("rec_keep_days") or 0)
            max_files = int(self.cfg.get("rec_max_files") or 0)
            max_mb = float(self.cfg.get("rec_max_mb") or 0)
        except (TypeError, ValueError):
            return []

        removed = []
        # 1) 过期目录（按日）
        if keep_days > 0:
            cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
            for name in sorted(os.listdir(base)):
                p = os.path.join(base, name)
                if not os.path.isdir(p):
                    continue
                try:
                    datetime.date.fromisoformat(name)
                except ValueError:
                    continue
                if name < cutoff:
                    for f in sorted(os.listdir(p)):
                        try:
                            os.remove(os.path.join(p, f))
                            removed.append(f"{name}/{f}")
                        except OSError:
                            pass
                    try:
                        os.rmdir(p)
                    except OSError:
                        pass
        # 2) 配额：文件数 / 总大小
        files = self._all_files()
        if max_files > 0 and len(files) > max_files:
            for path, _mt, _sz in files[: len(files) - max_files]:
                try:
                    os.remove(path)
                    removed.append(os.path.relpath(path, base).replace(os.sep, "/"))
                except OSError:
                    pass
            files = self._all_files()
        if max_mb > 0:
            limit = max_mb * 1024 * 1024
            total = sum(sz for _p, _m, sz in files)
            i = 0
            while total > limit and i < len(files):
                try:
                    os.remove(files[i][0])
                    total -= files[i][2]
                    removed.append(os.path.relpath(files[i][0], base).replace(os.sep, "/"))
                except OSError:
                    pass
                i += 1
        if removed:
            logger.info("录音清理：删除 %d 个文件（保留 %s 天 / 最多 %s 个 / %s MB）",
                        len(removed), keep_days, max_files or "-", max_mb or "-")
        return removed

    def clear_all(self):
        """清空所有录音（删除按日目录下的全部 WAV，再删空目录）。用于「立即清理」。

        与 prune() 的区别：prune 只按保留天数/配额删"最旧的"，对近期的录音无影响；
        本方法直接清空全部历史文件，符合"立即清理"按钮的预期（用户点它就是要清掉录音）。
        """
        base = self.base_dir()
        removed = 0
        if not os.path.isdir(base):
            return 0
        for day in sorted(os.listdir(base)):
            d = os.path.join(base, day)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                    removed += 1
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass
        if removed:
            logger.info("录音已清空：删除 %d 个文件", removed)
        return removed

    def _all_files(self):
        base = self.base_dir()
        out = []
        if not os.path.isdir(base):
            return out
        for day in sorted(os.listdir(base)):
            d = os.path.join(base, day)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not f.lower().endswith(".wav"):
                    continue
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                    out.append((p, st.st_mtime, st.st_size))
                except OSError:
                    pass
        out.sort(key=lambda x: x[1])     # 旧 -> 新
        return out

    def stats(self):
        files = self._all_files()
        total = sum(sz for _p, _m, sz in files)
        recent = []
        for p, mt, sz in reversed(files[-10:]):
            rel = os.path.relpath(p, self.base_dir()).replace(os.sep, "/")
            recent.append({
                "file": rel,
                "size": sz,
                "mtime": datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S"),
                "ts": int(mt * 1000),
            })
        return {
            "enabled": self.enabled(),
            "ready": self.ready,
            "count": len(files),
            "total_mb": round(total / 1024 / 1024, 2),
            "buffer_sec": round(self.ring.seconds(), 1) if self.ring else 0,
            "samplerate": self.samplerate,
            "channels": self.channels,
            "recording": len(self.pending) > 0,
            "saved_count": self.saved_count,
            "last_file": self.last_file,
            "last_error": self.last_error,
            "cut_on_alarm": self.cut_on_alarm(),       # 报警触发即截断（仅留触发前音频）
            "recent": recent,
        }
