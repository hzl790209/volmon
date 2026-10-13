"""日志文件**按日切割**与过期清理。

命名约定（当前文件不带日期、归档文件带日期，扩展名保持不变）：

    data/volmon.log              <- 今天（实时写入）
    data/volmon_2026-09-26.log   <- 昨天归档
    data/alerts.csv              <- 今天（实时追加）
    data/alerts_2026-09-26.csv   <- 昨天归档
    data/restart.log / restart_2026-09-26.log

超过保留天数（默认 30 天）的归档文件会被自动删除。
采样数据不参与切割，仍统一放在 data/volmon.db 单库里（用「日志维护」清理）。
"""
import datetime
import logging
import os
import re

DEFAULT_KEEP_DAYS = 30

_DAY_RE = re.compile(r"^(?P<stem>.+)_(?P<day>\d{4}-\d{2}-\d{2})(?P<ext>\.\w+)?$")


def day_str(dt=None):
    """datetime/date -> 'YYYY-MM-DD'（默认取当前本地时间）。"""
    dt = dt or datetime.datetime.now()
    return dt.strftime("%Y-%m-%d")


def archive_path(path, day):
    """data/volmon.log + '2026-09-26' -> data/volmon_2026-09-26.log"""
    d = os.path.dirname(path)
    stem, ext = os.path.splitext(os.path.basename(path))
    return os.path.join(d, f"{stem}_{day}{ext}")


def archive_day(path):
    """从归档文件名里解析出日期字符串；不是归档文件则返回 None。"""
    m = _DAY_RE.match(os.path.basename(path))
    if not m:
        return None
    try:  # 校验是不是合法日期，避免把 volmon_abc.log 之类误判
        datetime.date.fromisoformat(m.group("day"))
    except ValueError:
        return None
    return m.group("day")


def list_archives(path):
    """列出 path 对应的所有按日归档文件 -> [(day, abs_path)]，按日期升序。"""
    if not path:
        return []
    d = os.path.dirname(os.path.abspath(path))
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    out = []
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if not (name.startswith(stem + "_") and name.endswith(ext)):
            continue
        day = archive_day(name)
        if day:
            out.append((day, os.path.join(d, name)))
    out.sort(key=lambda x: x[0])
    return out


def file_mtime_day(path):
    """文件最后修改日期字符串；文件不存在返回 None。"""
    try:
        return day_str(datetime.datetime.fromtimestamp(os.path.getmtime(path)))
    except OSError:
        return None


def archive_file(path, day, keep_days=DEFAULT_KEEP_DAYS):
    """把 path 归档为 <stem>_<day><ext>（覆盖同名归档），随后清理过期文件。"""
    if not os.path.exists(path):
        return None
    dst = archive_path(path, day)
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.abspath(dst) != os.path.abspath(path):
        if os.path.exists(dst):
            os.remove(dst)
        os.rename(path, dst)
    prune_archives(path, keep_days)
    return dst


def rotate_file_if_stale(path, keep_days=DEFAULT_KEEP_DAYS, now=None):
    """若 path 存在且**最后修改日期不是今天**，先归档为那天的文件。

    用于进程启动时把"昨天遗留的当前文件"归位，保证 path 永远只装今天的内容。
    返回归档后的路径；无需切割返回 None。
    """
    if not path or not os.path.exists(path):
        return None
    now = now or datetime.datetime.now()
    mday = file_mtime_day(path)
    if not mday or mday == day_str(now):
        return None
    return archive_file(path, mday, keep_days)


def prune_archives(path, keep_days=DEFAULT_KEEP_DAYS, now=None):
    """删除超过保留天数的归档文件，返回被删除的路径列表。"""
    try:
        keep = int(keep_days or 0)
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return []
    now = now or datetime.datetime.now()
    cutoff = (now.date() - datetime.timedelta(days=keep)).isoformat()
    deleted = []
    for day, p in list_archives(path):
        if day < cutoff:  # 'YYYY-MM-DD' 字符串比较即等价于日期比较
            try:
                os.remove(p)
                deleted.append(p)
            except OSError:
                pass
    return deleted


class DailyRotatingFileHandler(logging.FileHandler):
    """每天一个日志文件：日常写 <base>，跨日时把昨天的内容归档为 <stem>_<date><ext>。

    与标准库 TimedRotatingFileHandler 的区别：归档文件名形如 volmon_2026-09-26.log
    （保留扩展名、日期一眼可见），且保留天数可运行时动态调整。
    """

    def __init__(self, path, keep_days=DEFAULT_KEEP_DAYS, encoding="utf-8",
                 now_fn=None, keep_days_getter=None):
        self.base_path = os.path.abspath(path)
        self.keep_days = keep_days
        self._keep_days_getter = keep_days_getter
        self._now_fn = now_fn or datetime.datetime.now
        os.makedirs(os.path.dirname(self.base_path) or ".", exist_ok=True)
        self.current_day = day_str(self._now_fn())
        # 启动时先把"昨天遗留的当前文件"归位，避免今天的内容追加到昨天的文件里
        rotate_file_if_stale(self.base_path, self._keep_days(), now=self._now_fn())
        super().__init__(self.base_path, mode="a", encoding=encoding)
        prune_archives(self.base_path, self._keep_days())

    def _keep_days(self):
        if self._keep_days_getter:
            try:
                return int(self._keep_days_getter() or 0)
            except (TypeError, ValueError):
                return 0
        return self.keep_days

    # ---- 切割 ----
    def should_rollover(self):
        return day_str(self._now_fn()) != self.current_day

    def do_rollover(self):
        """关闭当前流 -> 归档为昨天的文件 -> 重新打开 -> 清理过期归档。"""
        self.acquire()
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            dst = archive_path(self.base_path, self.current_day)
            if os.path.exists(self.base_path):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(self.base_path, dst)
            self.current_day = day_str(self._now_fn())
            self.stream = self._open()
            prune_archives(self.base_path, self._keep_days())
            return dst
        finally:
            self.release()

    def emit(self, record):
        try:
            if self.should_rollover():
                self.do_rollover()
        except Exception:
            pass
        logging.FileHandler.emit(self, record)


def file_info(path):
    """单个日志文件的展示信息（不存在返回 None）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        st = os.stat(path)
        return {
            "name": os.path.basename(path),
            "path": path,
            "day": archive_day(path),
            "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except OSError:
        return None


def log_files(paths):
    """给定若干"当前文件路径"，返回当前文件 + 各自归档文件的展示列表。"""
    out = []
    for p in paths:
        if not p:
            continue
        info = file_info(p)
        out.append({
            "base": os.path.basename(p),
            "current": info,
            "archives": [f for f in (file_info(a) for _, a in reversed(list_archives(p))) if f],
        })
    return out
