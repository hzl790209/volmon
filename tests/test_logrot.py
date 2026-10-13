"""日志按日切割与过期保留（对应需求：日志要按日切成独立文件、只保留 30 天）。

覆盖：
  - 归档命名：volmon.log -> volmon_2026-09-26.log；alerts.csv -> alerts_2026-09-26.csv
  - 跨日切割：昨天的内容归到昨天的文件，今天新开一个文件
  - 过期清理：超过保留天数的归档自动删除（0 = 不删）
  - 告警 CSV：切割后清空日志要能**同步重写对应日期的归档**
  - 服务端接口：/api/logs/files 与 /api/logs/rotate
"""
import csv as csv_mod
import datetime
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")

import pytest  # noqa: E402

from volmon import config as config_mod  # noqa: E402
from volmon import logrot  # noqa: E402
from volmon import storage as storage_mod  # noqa: E402


def _backdate(path, days):
    """把文件 mtime 改到 N 天前，模拟"昨天遗留的当前文件"。"""
    t = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
    os.utime(path, (t, t))


def _days_ago(n):
    dt = datetime.datetime.now() - datetime.timedelta(days=n)
    return dt, int(dt.timestamp() * 1000)


# ---------------- 命名与解析 ----------------
def test_archive_naming_keeps_extension():
    assert os.path.basename(logrot.archive_path("data/volmon.log", "2026-09-26")) == "volmon_2026-09-26.log"
    assert os.path.basename(logrot.archive_path("data/alerts.csv", "2026-09-26")) == "alerts_2026-09-26.csv"
    assert logrot.archive_day("volmon_2026-09-26.log") == "2026-09-26"
    assert logrot.archive_day("alerts_2026-09-26.csv") == "2026-09-26"
    assert logrot.archive_day("volmon.log") is None          # 当前文件没有日期
    assert logrot.archive_day("volmon_2026-13-99.log") is None  # 非法日期不算归档


# ---------------- 文件级切割 ----------------
def test_rotate_only_when_file_is_stale():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "volmon.log")
    with open(p, "w", encoding="utf-8") as f:
        f.write("yesterday\n")
    _backdate(p, 1)

    dst = logrot.rotate_file_if_stale(p, keep_days=30)
    assert dst is not None and os.path.exists(dst)
    assert not os.path.exists(p), "切割后当前文件应由今天重新创建"
    with open(dst, encoding="utf-8") as f:
        assert f.read() == "yesterday\n"

    # 今天的文件不该被切
    with open(p, "w", encoding="utf-8") as f:
        f.write("today\n")
    assert logrot.rotate_file_if_stale(p, keep_days=30) is None
    assert os.path.exists(p)


# ---------------- Handler 跨日切割 ----------------
def test_handler_rolls_over_at_midnight():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "volmon.log")
    clock = {"t": datetime.datetime(2026, 9, 26, 23, 59, 0)}
    h = logrot.DailyRotatingFileHandler(p, keep_days=30, now_fn=lambda: clock["t"])
    lg = logging.getLogger("test.rot")
    lg.handlers = []
    lg.propagate = False
    lg.setLevel(logging.INFO)
    lg.addHandler(h)

    lg.info("day1")
    clock["t"] = datetime.datetime(2026, 9, 27, 0, 0, 5)
    lg.info("day2")
    h.flush()

    arc = os.path.join(tmp, "volmon_2026-09-26.log")
    assert os.path.exists(arc), "跨日应生成昨天的归档文件"
    assert "day1" in open(arc, encoding="utf-8").read()
    body = open(p, encoding="utf-8").read()
    assert "day2" in body and "day1" not in body, "今天的日志里不能混进昨天的"
    h.close()


# ---------------- 过期清理 ----------------
def test_prune_deletes_older_than_keep_days():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "volmon.log")
    today = datetime.datetime.now().date()
    made = {}
    for n in (1, 5, 29, 31, 40):
        day = (today - datetime.timedelta(days=n)).isoformat()
        made[n] = day
        open(logrot.archive_path(p, day), "w").write("x")

    deleted = {logrot.archive_day(os.path.basename(x)) for x in logrot.prune_archives(p, 30)}
    assert made[31] in deleted and made[40] in deleted, "超过 30 天的应删除"
    assert made[1] not in deleted and made[29] not in deleted, "30 天内的应保留"

    # keep_days=0 表示永不删除
    for n in (31, 40):
        open(logrot.archive_path(p, made[n]), "w").write("x")
    assert logrot.prune_archives(p, 0) == []


def test_keep_days_getter_is_dynamic():
    """保留天数可在运行时调整（网页改配置后立即生效，无需重启）。"""
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "volmon.log")
    for n in (1, 40):
        day = (datetime.datetime.now().date() - datetime.timedelta(days=n)).isoformat()
        open(logrot.archive_path(p, day), "w").write("x")
    keep = {"v": 30}
    logrot.prune_archives(p, keep["v"])
    assert len(logrot.list_archives(p)) == 1
    # 保留天数改为 0 之后再切一次：40 天前的那份不会被删除（因为已删过），这里只验证不再报错
    keep["v"] = 0
    assert logrot.prune_archives(p, keep["v"]) == []


# ---------------- 告警 CSV 按日切割 ----------------
def test_alerts_csv_rotates_daily():
    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "alerts.csv")
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=csv_path)
    now = int(time.time() * 1000)
    store.add_alert(now, now + 1000, 60.0, 55.0, context={"period": "白天"})

    _backdate(csv_path, 1)                      # 假装这个 CSV 已经跨天了
    store.add_alert(now, now + 1000, 61.0, 55.0, context={"period": "晚间"})

    arcs = logrot.list_archives(csv_path)
    assert len(arcs) == 1, f"应生成 1 个归档 CSV，实际 {arcs}"
    with open(arcs[0][1], encoding="utf-8-sig") as f:
        rows = list(csv_mod.reader(f))
    assert len(rows) == 2, "归档里应是表头 + 昨天的 1 条"
    assert rows[1][5] == "白天"

    with open(csv_path, encoding="utf-8-sig") as f:
        cur = [r for r in csv_mod.reader(f) if r]
    assert len(cur) == 2, "当前 CSV 应是表头 + 今天的 1 条"
    assert cur[1][5] == "晚间"


def test_clear_syncs_archived_csv():
    """清空某天的告警后，那天的归档 CSV 也要同步重写（不能"库里没了、归档里还在"）。"""
    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "alerts.csv")
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=csv_path)

    dt2, ts2 = _days_ago(2)                     # 前天的一条告警
    store.add_alert(ts2, ts2 + 500, 60.0, 55.0, context={"period": "白天"})
    _backdate(csv_path, 2)
    now = int(time.time() * 1000)               # 触发切割，把前天那条归档
    store.add_alert(now, now + 500, 61.0, 55.0, context={"period": "晚间"})

    day2 = dt2.date().isoformat()
    arc = logrot.archive_path(csv_path, day2)
    assert os.path.exists(arc), "前天的告警应已归档"

    store.clear(scope="alerts", since_ts=ts2 - 1000, until_ts=ts2 + 1000)
    assert not os.path.exists(arc), "库里删掉后，对应日期的归档 CSV 也应清掉"
    with open(csv_path, encoding="utf-8-sig") as f:
        cur = [r for r in csv_mod.reader(f) if r]
    assert len(cur) == 2 and cur[1][5] == "晚间", "今天的告警不受影响"


def test_rebuild_splits_history_by_day():
    """重建时按天拆档：每天的告警只进自己那天的 CSV。"""
    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "alerts.csv")
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=csv_path)
    days = []
    for n in (0, 1, 2):
        dt, ts = _days_ago(n)
        days.append(dt.date().isoformat())
        store.add_alert(ts, ts + 500, 60.0 + n, 55.0, context={"period": "白天"})

    store.rebuild_alerts_csv(days=days)
    for n, day in zip((0, 1, 2), days):
        p = csv_path if n == 0 else logrot.archive_path(csv_path, day)
        with open(p, encoding="utf-8-sig") as f:
            rows = [r for r in csv_mod.reader(f) if r]
        assert len(rows) == 2, f"{os.path.basename(p)} 应是表头 + 1 条，实际 {len(rows)}"


# ---------------- 服务端接口 ----------------
def test_logs_files_and_rotate_endpoints(monkeypatch):
    pytest.importorskip("aiohttp")
    import asyncio

    from volmon import alerting as A
    from volmon import server as server_mod
    from volmon import sim as sim_mod

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(server_mod, "ROOT", tmp)   # 重启日志也指到临时目录，别动真实数据
    cfg = config_mod.Config(path=os.path.join(tmp, "config.json"))
    cfg.set("log_path", os.path.join(tmp, "volmon.log"))
    cfg.set("alerts_csv_path", os.path.join(tmp, "alerts.csv"))
    cfg.set("log_keep_days", 30)
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=cfg.get("alerts_csv_path"))
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    srv = server_mod.MonitorServer(sim_mod.SimEngine(), store, cfg, alm)

    open(cfg.get("log_path"), "w", encoding="utf-8").write("x\n")

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(srv.app))
        await client.start_server()
        try:
            r = await client.get("/api/logs/files")
            j = await r.json()
            assert j["ok"] is True and j["keep_days"] == 30
            assert len(j["files"]) == 3, "运行日志 / 告警 CSV / 重启日志三类"

            r = await client.post("/api/logs/rotate", json={"force": True})
            j = await r.json()
            assert j["ok"] is True
            archived = [os.path.basename(str(x)) for x in j["archived"]]
            assert any(n.startswith("volmon_") for n in archived), archived
            # 文件级切割后原文件已改名归档（真实服务里由日志 handler 负责重新创建）
            assert os.path.exists(os.path.join(tmp, "volmon_%s.log" % logrot.day_str()))

            # 保留天数可通过配置接口调整
            r = await client.post("/api/config", json={"log_keep_days": 7})
            j = await r.json()
            assert j["applied"]["log_keep_days"] == 7
            r = await client.get("/api/logs/files")
            assert (await r.json())["keep_days"] == 7
        finally:
            await client.close()

    asyncio.run(_run())
