"""存储层：SQLite 保存采样点、超量告警事件；并提供 CSV 导出。

本地文件：
  - samples / alerts 存 SQLite（data/volmon.db，单库不切割）
  - 超量告警**同时追加写入** CSV（默认 data/alerts.csv，UTF-8 BOM），
    便于用户直接用 Excel / 文本工具查看，不依赖数据库。
  - 告警 CSV **按日切割**：当前文件只装今天的告警，跨日自动归档为
    data/alerts_YYYY-MM-DD.csv；超过 log_keep_days（默认 30 天）的归档自动删除。
"""
import csv
import datetime
import logging
import os
import sqlite3
import threading
import time

from volmon import logrot

logger = logging.getLogger("volmon.storage")


def _fmt(ms):
    """毫秒时间戳 -> 本地可读时间字符串。"""
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _day_of_ms(ms):
    """毫秒时间戳 -> 'YYYY-MM-DD'（用于把告警归到对应日期的 CSV）。"""
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


class Storage:
    def __init__(self, path="data/volmon.db", alerts_csv=None,
                 csv_rotate_daily=True, csv_keep_days=logrot.DEFAULT_KEEP_DAYS):
        self.path = path
        self.alerts_csv = alerts_csv  # 超量告警的本地 CSV 落盘路径（None 则不写）
        self.csv_rotate_daily = bool(csv_rotate_daily)  # 告警 CSV 是否按日切割
        self.csv_keep_days = csv_keep_days              # 归档 CSV 保留天数（0 = 不删）
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.conn = self._open_db(path)
        self.conn.row_factory = sqlite3.Row
        self._init()
        self.rotate_alerts_csv_if_stale()  # 启动时把"昨天遗留的 CSV"归档

    def _open_db(self, path):
        """打开 SQLite 库；若文件已损坏则自动备份并创建新库，避免启动崩溃。"""
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            # 做一次轻量查询来验证库文件是否完好（connect 成功不代表文件合法）
            conn.execute("SELECT name FROM sqlite_master LIMIT 1")
            return conn
        except sqlite3.DatabaseError as e:
            if not os.path.exists(path):
                raise
            backup = f"{path}.corrupt.{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            try:
                os.rename(path, backup)
            except OSError as ren_e:
                raise sqlite3.DatabaseError(
                    f"volmon.db 已损坏且无法备份: {ren_e}"
                ) from e
            logger.warning(
                "volmon.db 已损坏（%s），已备份到 %s，将创建新库继续运行",
                e, backup
            )
            # 保险起见也打印到 stdout，方便打包版用户在终端/Console 看到
            print(f"[storage] volmon.db 已损坏，已备份到 {backup}，将创建新库继续运行")
            return sqlite3.connect(path, check_same_thread=False)

    def set_csv_retention(self, rotate_daily=None, keep_days=None):
        """运行时调整告警 CSV 的切割/保留策略（网页改配置后立即生效）。"""
        if rotate_daily is not None:
            self.csv_rotate_daily = bool(rotate_daily)
        if keep_days is not None:
            try:
                self.csv_keep_days = int(keep_days)
            except (TypeError, ValueError):
                pass

    def _init(self):
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                """CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    raw_db REAL,
                    db REAL,
                    device TEXT
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_ts INTEGER,
                    end_ts INTEGER,
                    peak_db REAL,
                    threshold_db REAL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_start ON alerts(start_ts)")
            # 迁移：为历史库补上"当时生效策略"相关列（老库升级不会丢数据）
            cols = {r["name"] for r in c.execute("PRAGMA table_info(alerts)").fetchall()}
            for col, ddl in (
                ("period", "TEXT"),
                ("schedule_applied", "INTEGER"),
                ("alarm_enabled", "INTEGER"),
                ("alarm_duration_sec", "REAL"),
                ("alarm_cooldown_sec", "REAL"),
                ("audio_file", "TEXT"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE alerts ADD COLUMN {col} {ddl}")
            self.conn.commit()

    def add_sample(self, ts, raw_db, db, device):
        with self.lock:
            self.conn.execute(
                "INSERT INTO samples (ts, raw_db, db, device) VALUES (?,?,?,?)",
                (ts, raw_db, db, str(device)),
            )
            self.conn.commit()

    # 告警 CSV 列（含"当时生效的策略"信息，便于事后追溯）
    ALERT_CSV_HEADER = [
        "start_time", "end_time", "peak_db", "threshold_db", "duration_s",
        "period", "schedule_applied", "alarm_enabled", "alarm_duration_s", "alarm_cooldown_s",
        "audio_file",
    ]

    def add_alert(self, start_ts, end_ts, peak_db, threshold_db, context=None):
        """记录一次超量告警。

        context: 当时生效的策略快照 {"period", "schedule_applied",
                 "alarm_enabled", "alarm_duration_sec", "alarm_cooldown_sec"}，
                 一并入库与写入 CSV，便于事后回答"当时用的是哪套策略、报警开没开"。
        """
        ctx = context or {}
        period = ctx.get("period", "")
        sched_on = 1 if ctx.get("schedule_applied") else 0
        alarm_on = 1 if ctx.get("alarm_enabled") else 0
        dur = ctx.get("alarm_duration_sec", None)
        cd = ctx.get("alarm_cooldown_sec", None)
        audio = ctx.get("audio_file", None)
        with self.lock:
            self.conn.execute(
                "INSERT INTO alerts (start_ts, end_ts, peak_db, threshold_db,"
                " period, schedule_applied, alarm_enabled, alarm_duration_sec, alarm_cooldown_sec,"
                " audio_file)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (start_ts, end_ts, peak_db, threshold_db, period, sched_on, alarm_on, dur, cd, audio),
            )
            self.conn.commit()
        self._append_alert_csv(start_ts, end_ts, peak_db, threshold_db, ctx)

    def link_audio(self, trigger_ts, rel_path):
        """把录音文件回填到**触发时刻所在的那次**超量告警上。

        录音是在报警触发时开始、post 秒后才写完的，而告警记录可能在超标结束时才落库，
        所以没法在落库时就填好文件名；这里用触发时刻反查属于哪一段超标。
        匹配不到（罕见）就挂到最近一条 60 秒内的告警上，保证页面上一定能听到。
        """
        if not rel_path:
            return False
        with self.lock:
            cur = self.conn.execute(
                "SELECT id FROM alerts WHERE start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)"
                " ORDER BY start_ts DESC LIMIT 1",
                (int(trigger_ts), int(trigger_ts)),
            )
            row = cur.fetchone()
            if row is None:
                cur = self.conn.execute(
                    "SELECT id FROM alerts WHERE start_ts >= ? ORDER BY start_ts DESC LIMIT 1",
                    (int(trigger_ts) - 60000,),
                )
                row = cur.fetchone()
            if row is None:
                return False
            self.conn.execute("UPDATE alerts SET audio_file=? WHERE id=?", (rel_path, row["id"]))
            self.conn.commit()
        self.rebuild_alerts_csv(days=self._affected_days(None, None))
        return True

    def _alert_row_values(self, r, ctx=None):
        """一条告警 -> CSV 行（ctx 缺失时从 DB 字段补）。"""
        ctx = ctx or {}

        def pick(key, col):
            v = ctx.get(key, None)
            return v if v is not None else r[col] if col in r.keys() else None

        period = pick("period", "period") or ""
        sched_on = pick("schedule_applied", "schedule_applied")
        alarm_on = pick("alarm_enabled", "alarm_enabled")
        dur = pick("alarm_duration_sec", "alarm_duration_sec")
        cd = pick("alarm_cooldown_sec", "alarm_cooldown_sec")
        audio = ctx.get("audio_file", None)
        if audio is None and "audio_file" in r.keys():
            audio = r["audio_file"]
        return [
            _fmt(r["start_ts"]),
            _fmt(r["end_ts"]),
            f"{float(r['peak_db']):.2f}",
            f"{float(r['threshold_db']):.2f}",
            f"{(r['end_ts'] - r['start_ts']) / 1000.0:.1f}" if r["end_ts"] else "",
            period,
            "是" if sched_on else "否",
            "是" if alarm_on else "否",
            "" if dur is None else f"{float(dur):.0f}",
            "" if cd is None else f"{float(cd):.0f}",
            audio or "",
        ]

    def rotate_alerts_csv_if_stale(self):
        """告警 CSV 按日切割：当前文件不是今天的就先归档。

        返回归档后的路径；无需切割返回 None。
        """
        if not self.alerts_csv or not self.csv_rotate_daily:
            return None
        return logrot.rotate_file_if_stale(self.alerts_csv, self.csv_keep_days)

    def _append_alert_csv(self, start_ts, end_ts, peak_db, threshold_db, context=None):
        """每条超量告警追加一行到本地 CSV，便于用 Excel / 文本工具直接查看。

        写入前先做按日切割检查：跨日时把昨天的 CSV 归档为 alerts_YYYY-MM-DD.csv，
        保证当前文件里只有今天的告警。
        """
        if not self.alerts_csv:
            return
        try:
            d = os.path.dirname(self.alerts_csv)
            if d:
                os.makedirs(d, exist_ok=True)
            self.rotate_alerts_csv_if_stale()
            new_file = not os.path.exists(self.alerts_csv)
            row = dict(start_ts=start_ts, end_ts=end_ts, peak_db=peak_db, threshold_db=threshold_db)
            with open(self.alerts_csv, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(self.ALERT_CSV_HEADER)
                w.writerow(self._alert_row_values(row, context))
        except Exception as e:  # 落盘失败不应影响监测
            print(f"[storage] 写告警 CSV 失败: {e}")

    def _write_alerts_csv(self, path, rows):
        """把告警行写入 CSV（UTF-8 BOM，Excel 可直接打开）。"""
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(self.ALERT_CSV_HEADER)
            for r in rows:
                # rows 可能来自 get_alerts()（无 ctx）或直接是 sqlite3.Row
                w.writerow(self._alert_row_values(r))
        return len(rows)

    def _all_alert_days(self):
        """库内告警涉及到的所有日期（升序），用于一次性生成各天的归档 CSV。"""
        rows = self.get_alerts(0)
        days = sorted({_day_of_ms(r["start_ts"]) for r in rows if _day_of_ms(r["start_ts"])})
        return days

    def ensure_alerts_csv(self):
        """确保本地 CSV 存在且表头为**当前版本**；否则用库内历史告警重建。

        新增策略列后，老版本 CSV（5 列）会与新行（10 列）错位，
        因此启动时若发现表头不是当前版本，自动按库里的数据重写（历史数据不丢）。
        开启按日切割后，会**顺带把库里历史告警按天拆成各自的归档 CSV**。
        """
        if not self.alerts_csv:
            return 0
        if not os.path.exists(self.alerts_csv):
            return self.rebuild_alerts_csv(days=self._all_alert_days() if self.csv_rotate_daily else None)
        try:
            with open(self.alerts_csv, "r", encoding="utf-8-sig") as f:
                header = f.readline().strip()
            if header != ",".join(self.ALERT_CSV_HEADER):
                days = self._all_alert_days() if self.csv_rotate_daily else None
                n = self.rebuild_alerts_csv(days=days)
                print(f"[storage] 告警 CSV 表头已升级为含策略列的版本（今日 {n} 行"
                      f"{f'；历史按日归档 {len(days)} 天' if days else ''}，历史数据保留）")
                return n
        except Exception as e:
            print(f"[storage] 检查告警 CSV 表头失败: {e}")
        return 0

    def export_alerts_csv(self, out_path, since_ts=0):
        """导出超量告警为 CSV（UTF-8 BOM，Excel 可直接打开），返回条数。"""
        return self._write_alerts_csv(out_path, self.get_alerts(since_ts))

    def get_samples(self, since_ts):
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, raw_db, db FROM samples WHERE ts >= ? ORDER BY ts ASC",
                (since_ts,),
            ).fetchall()
        return [{"t": r["ts"], "raw_db": r["raw_db"], "db": r["db"]} for r in rows]

    def _alert_dict(self, r):
        return {
            "start_ts": r["start_ts"],
            "end_ts": r["end_ts"],
            "peak_db": r["peak_db"],
            "threshold_db": r["threshold_db"],
            "duration_s": round((r["end_ts"] - r["start_ts"]) / 1000.0, 1) if r["end_ts"] else None,
            "period": r["period"] or "",
            "schedule_applied": bool(r["schedule_applied"]),
            "alarm_enabled": bool(r["alarm_enabled"]),
            "alarm_duration_sec": r["alarm_duration_sec"],
            "alarm_cooldown_sec": r["alarm_cooldown_sec"],
            "audio_file": r["audio_file"] or "",
        }

    _ALERT_COLS = ("start_ts, end_ts, peak_db, threshold_db, period, schedule_applied,"
                   " alarm_enabled, alarm_duration_sec, alarm_cooldown_sec, audio_file")

    def get_alerts(self, since_ts):
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._ALERT_COLS} FROM alerts WHERE start_ts >= ? ORDER BY start_ts DESC",
                (since_ts,),
            ).fetchall()
        return [self._alert_dict(r) for r in rows]

    def get_alerts_range(self, since_ts, until_ts):
        """取 [since_ts, until_ts) 区间的告警（正序），供日报/导出使用。"""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._ALERT_COLS} FROM alerts"
                " WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts ASC",
                (since_ts, until_ts),
            ).fetchall()
        return [self._alert_dict(r) for r in rows]

    def export_alerts_range_csv(self, out_path, since_ts, until_ts):
        """导出指定时间区间的告警为 CSV，返回条数（日报附件用）。"""
        return self._write_alerts_csv(out_path, self.get_alerts_range(since_ts, until_ts))

    # ---------- 清空日志 ----------
    def clear(self, scope="all", since_ts=None, until_ts=None):
        """清空日志。

        scope: "all" | "samples" | "alerts"
        since_ts / until_ts: 毫秒时间戳闭区间边界；None 表示不限。
        返回 {"samples": 删除条数, "alerts": 删除条数}。
        """
        scope = (scope or "all").lower()
        if scope not in ("all", "samples", "alerts"):
            raise ValueError(f"未知清空范围: {scope}")

        def where(col):
            sql, args = "", []
            if since_ts is not None:
                sql += f" AND {col} >= ?"
                args.append(int(since_ts))
            if until_ts is not None:
                sql += f" AND {col} <= ?"
                args.append(int(until_ts))
            return sql, args

        deleted = {"samples": 0, "alerts": 0}
        with self.lock:
            c = self.conn.cursor()
            if scope in ("all", "samples"):
                sql, args = where("ts")
                c.execute(f"DELETE FROM samples WHERE 1=1{sql}", args)
                deleted["samples"] = c.rowcount if c.rowcount and c.rowcount > 0 else 0
            if scope in ("all", "alerts"):
                sql, args = where("start_ts")
                c.execute(f"DELETE FROM alerts WHERE 1=1{sql}", args)
                deleted["alerts"] = c.rowcount if c.rowcount and c.rowcount > 0 else 0
            self.conn.commit()
        # 告警清过之后，本地 CSV 必须与库保持一致（否则用户会看到"库里没了、CSV 里还在"）
        if scope in ("all", "alerts"):
            self.rebuild_alerts_csv(days=self._affected_days(since_ts, until_ts))
        return deleted

    def _affected_days(self, since_ts, until_ts):
        """清空操作可能影响到的日期列表（用于同步重写对应日期的归档 CSV）。"""
        if not self.alerts_csv:
            return []
        if since_ts is None and until_ts is None:
            # 全清：所有已有归档 + 今天
            return [d for d, _ in logrot.list_archives(self.alerts_csv)] + [logrot.day_str()]
        if since_ts is None:
            since_ts = 0
        if until_ts is None:
            until_ts = time.time() * 1000
        days = []
        d = datetime.datetime.fromtimestamp(float(since_ts) / 1000).date()
        end = datetime.datetime.fromtimestamp(float(until_ts) / 1000).date()
        while d <= end and len(days) < 2000:  # 上限保护，避免异常时间导致死循环
            days.append(d.isoformat())
            d += datetime.timedelta(days=1)
        return days

    def rebuild_alerts_csv(self, days=None):
        """按库内现有告警**重写**本地 CSV（清空/删除后保持一致）。

        days: 需要一并重写的**归档日期**列表。清空一段时间范围时，
              这些天的归档 CSV 也要跟着重写，否则会出现"库里没了、归档 CSV 里还在"。
        返回今日 CSV 的行数。
        """
        if not self.alerts_csv:
            return 0
        all_rows = list(reversed(self.get_alerts(0)))  # get_alerts 倒序，落文件用正序
        if self.csv_rotate_daily:
            today = logrot.day_str()
            rows = [r for r in all_rows if _day_of_ms(r["start_ts"]) == today]
        else:
            rows = all_rows
        n = self._write_alerts_csv(self.alerts_csv, rows)
        for day in (days or []):
            if self.csv_rotate_daily and day == logrot.day_str():
                continue  # 今天的内容只在当前文件里，不重复生成"今天的归档"
            p = logrot.archive_path(self.alerts_csv, day)
            day_rows = [r for r in all_rows if _day_of_ms(r["start_ts"]) == day]
            if day_rows:
                self._write_alerts_csv(p, day_rows)
            elif os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return n

    def counts(self):
        """当前库内条数，用于清空前后对比展示。"""
        with self.lock:
            s = self.conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
            a = self.conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
        return {"samples": s, "alerts": a}

    def export_csv(self, out_path, since_ts):
        rows = self.get_samples(since_ts)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_ms", "datetime", "raw_db", "db"])
            import datetime

            for r in rows:
                w.writerow(
                    [
                        r["t"],
                        datetime.datetime.fromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                        r["raw_db"],
                        r["db"],
                    ]
                )
        return len(rows)
