"""统一的本地存储配额：把「日志（运行日志 / 告警 CSV / 重启日志）+ 录音」纳入一个总池，
超过上限时按修改时间从最旧开始删除（LRU），避免把磁盘撑爆。

## 与既有配额的关系

- 录音自身的 `rec_max_mb` / `rec_max_files` / `rec_keep_days` 是"录音内部"的独立配额，
  仍照常生效；本模块是"**日志 + 录音**"的**合计**总闸门，二者互补。
- 本模块**不触及** `volmon.db`（SQLite 采样库持续被服务读写，删除/截断有风险）。
  要控制采样库体积，请用网页「日志维护（清空）」。
- 正在被当前进程写入的"今天"日志文件受保护，不会被删除（否则日志句柄会指向已删文件、
  归档逻辑会乱）。配额超限时优先删归档日志与历史录音，极端情况下若单个当前日志本身就
  超限，则只能提示、不强行删。

## 工作原理

`enforce(config)` 每 N 分钟由服务主循环调用一次：
1. 枚举所有日志类文件（当前 + 按日归档）+ 所有录音 WAV，统计合计字节数；
2. 若合计 ≤ `storage_max_mb`（0 = 不限制）直接返回；
3. 否则按 mtime 升序（最旧优先）逐个删除，直到低于上限为止；跳过受保护的当前日志文件。
"""
import logging
import os

from volmon import logrot as logrot_mod
from volmon import paths as paths_mod

logger = logging.getLogger("volmon.storage_quota")

# 数据/日志根目录：源码运行=项目根；打包后=~/Library/Application Support/volmon-mac
ROOT = paths_mod.data_root()


def _abs(p):
    """把配置里的相对路径（相对 data_root）解析成绝对路径，兼容任意 CWD。"""
    if not p:
        return ""
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(ROOT, p))


def _current_log_paths(config):
    """当前进程正在写入的"今天"日志文件（受保护，不删除）。"""
    return [
        _abs(config.get("log_path")),
        _abs(config.get("alerts_csv_path")),
        _abs("data/restart.log"),
    ]


def _log_candidates(config):
    """所有日志类文件路径（当前文件 + 按日归档）。"""
    out = []
    seen = set()
    for raw in (config.get("log_path"), config.get("alerts_csv_path")):
        if not raw:
            continue
        ap = _abs(raw)
        if ap and ap not in seen:
            seen.add(ap)
            out.append(ap)
        try:
            for _day, a in logrot_mod.list_archives(ap):   # 必须用绝对路径，避免依赖 CWD
                aa = os.path.abspath(a)
                if aa not in seen:
                    seen.add(aa)
                    out.append(aa)
        except Exception:
            pass
    restart = _abs("data/restart.log")
    if restart and restart not in seen:
        seen.add(restart)
        out.append(restart)
    return out


def _rec_candidates(config):
    """递归列出录音目录下所有 WAV 的绝对路径。"""
    base = _abs(config.get("rec_dir") or os.path.join("data", "recordings"))
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
            out.append(os.path.join(d, f))
    return out


def collect(config):
    """汇总当前所有日志+录音文件信息。

    返回 (files, total_bytes)，其中
    files: [(abspath, mtime, size), ...] 按 mtime 升序（最旧在前）。
    """
    files = []
    for p in _log_candidates(config) + _rec_candidates(config):
        try:
            st = os.stat(p)
            files.append((p, st.st_mtime, st.st_size))
        except OSError:
            pass
    files.sort(key=lambda x: x[1])  # 旧 -> 新
    total = sum(sz for _p, _m, sz in files)
    return files, total


def enforce(config):
    """若日志+录音合计超过 `storage_max_mb`，从最旧开始删除直到低于上限。

    返回被删除的 [(relpath, size)] 列表；未超限或被保护文件无法删时返回 []（并视情况告警）。
    """
    try:
        cap = float(config.get("storage_max_mb") or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return []
    limit = cap * 1024 * 1024
    files, total = collect(config)
    if total <= limit:
        return []

    current = set(_current_log_paths(config))
    removed = []

    def _do_remove(p, sz):
        try:
            os.remove(p)
            rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
            removed.append((rel, sz))
            return True
        except OSError:
            return False

    # 第一轮：先删归档日志与历史录音（不碰当前日志文件），最旧优先
    ordered = sorted(files, key=lambda x: (0 if x[0] in current else 1, x[1]))
    i = 0
    while total > limit and i < len(ordered):
        p, _mt, sz = ordered[i]
        i += 1
        if p in current:           # 受保护的当前日志文件，跳过
            continue
        if _do_remove(p, sz):
            total -= sz

    # 第二轮：若仍超（极端：单个当前日志本身就超上限），再回退删当前日志
    if total > limit:
        for p, _mt, sz in ordered:
            if p not in current:
                continue
            if _do_remove(p, sz):
                total -= sz
            if total <= limit:
                break

    if removed:
        logger.info("存储配额：删除 %d 个最旧文件（日志+录音合计超 %d MB），剩余约 %.1f MB",
                    len(removed), cap, total / 1024 / 1024)
    else:
        logger.warning("存储配额：合计 %.1f MB 已超上限 %d MB，但全部为当前日志文件无法删除，"
                       "请手动在「日志维护」中清理", total / 1024 / 1024, cap)
    return removed


def stats(config):
    """给前端展示：合计大小、上限、日志/录音各自占用、文件数、是否超限。"""
    try:
        cap = int(config.get("storage_max_mb") or 0)
    except (TypeError, ValueError):
        cap = 0
    files, total = collect(config)
    rec_base = _abs(config.get("rec_dir") or os.path.join("data", "recordings")) + os.sep
    rec_bytes = 0
    n_rec = 0
    for p, _mt, sz in files:
        if p.startswith(rec_base):
            rec_bytes += sz
            n_rec += 1
    log_bytes = total - rec_bytes
    return {
        "ok": True,
        "cap_mb": cap,
        "total_mb": round(total / 1024 / 1024, 2),
        "log_mb": round(log_bytes / 1024 / 1024, 2),
        "rec_mb": round(rec_bytes / 1024 / 1024, 2),
        "count": len(files),
        "rec_count": n_rec,
        "over": bool(cap > 0 and total > cap * 1024 * 1024),
        "unlimited": cap <= 0,
    }
