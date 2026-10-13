"""每日报警日志邮件推送。

用 monkeypatch 替换 smtplib，全程**不真发信**、不联网。
覆盖：报表汇总、HTML/纯文本渲染、附件准备、SMTP_SSL/STARTTLS 路径、
无告警跳过、到点判断与"同一天不重复发"、接口 /api/mail 与 /api/mail/send。
"""
import asyncio
import datetime
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")

import pytest  # noqa: E402

from volmon import config as config_mod  # noqa: E402
from volmon import mailer  # noqa: E402
from volmon import storage as storage_mod  # noqa: E402


class FakeSMTP:
    """记录调用过程的假 SMTP（SSL 与明文共用一个类）。"""
    instances = []

    def __init__(self, host=None, port=None, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.logged_in = None
        self.starttls_called = False
        self.sent = []
        self.quitted = False
        FakeSMTP.instances.append(self)

    def starttls(self, *a, **k):
        self.starttls_called = True

    def login(self, user, pwd):
        self.logged_in = (user, pwd)

    def sendmail(self, frm, to, body):
        self.sent.append((frm, list(to), body))

    def quit(self):
        self.quitted = True


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    yield


def _mk(tmp, alerts=None):
    """建一个带数据的 store：alerts={几天前: 条数}"""
    alerts = alerts or {}
    csv_path = os.path.join(tmp, "alerts.csv")
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=csv_path)
    now = int(time.time() * 1000)
    for days_ago, n in alerts.items():
        for k in range(n):
            ts = now - int(days_ago) * 86400 * 1000 - k * 5000
            store.add_alert(ts, ts + 3000, 60.0 + k, 55.0,
                            context={"period": "晚间" if days_ago else "白天",
                                     "schedule_applied": True, "alarm_enabled": k == 0})
    return store


def _cfg(tmp, **over):
    cfg = config_mod.Config(path=os.path.join(tmp, "c.json"))
    cfg.set("mail_enabled", True)
    cfg.set("mail_host", "smtp.example.com")
    cfg.set("mail_port", 465)
    cfg.set("mail_ssl", True)
    cfg.set("mail_user", "me@example.com")
    cfg.set("mail_password", "authcode")
    cfg.set("mail_to", "boss@example.com, ops@example.com")
    cfg.set("mail_attach_csv", True)
    for k, v in over.items():
        cfg.set(k, v)
    return cfg


# ---------------- 报表内容 ----------------
def test_report_summarizes_one_day():
    tmp = tempfile.mkdtemp()
    store = _mk(tmp, {0: 3, 1: 2})
    rep = mailer.build_report(store, offset=1)   # 昨天：2 条
    assert rep["count"] == 2, rep
    assert rep["peak_db"] == 61.0
    assert rep["by_period"] == {"晚间": 2}
    assert rep["by_threshold"] == {"55": 2}
    assert rep["alarm_on_count"] == 1          # 只有 k==0 的那条开了声音报警
    assert rep["total_over_sec"] == 6.0
    assert rep["rows"][0]["start_ts"] < rep["rows"][1]["start_ts"]   # 正序

    today = mailer.build_report(store, offset=0)
    assert today["count"] == 3 and today["by_period"] == {"白天": 3}


def test_report_empty_day():
    tmp = tempfile.mkdtemp()
    store = storage_mod.Storage(os.path.join(tmp, "v.db"))
    rep = mailer.build_report(store, offset=1)
    assert rep["count"] == 0 and rep["peak_db"] is None
    assert "没有" in mailer.render_text(rep) or "无超量" in mailer.render_text(rep)
    assert "无超量告警" in mailer.subject_for(rep)


def test_render_contains_key_fields():
    tmp = tempfile.mkdtemp()
    store = _mk(tmp, {1: 2})
    rep = mailer.build_report(store, offset=1)
    html, text = mailer.render_html(rep), mailer.render_text(rep)
    for kw in ("超量次数", "峰值", "累计超标时长", "时段分布"):
        assert kw in html and kw in text
    assert "晚间" in html
    assert rep["day"] in html


# ---------------- 发送 ----------------
def test_send_uses_ssl_and_attaches_csv():
    tmp = tempfile.mkdtemp()
    store = _mk(tmp, {1: 2})
    cfg = _cfg(tmp)
    res = mailer.send_daily_report(cfg, store)
    assert res["ok"] is True, res
    assert res["count"] == 2 and res["to"] == ["boss@example.com", "ops@example.com"]

    s = FakeSMTP.instances[0]
    assert (s.host, s.port) == ("smtp.example.com", 465)
    assert s.logged_in == ("me@example.com", "authcode")
    assert s.quitted is True
    body = s.sent[0][2]
    assert "alerts_" in body and ".csv" in body, "应带 CSV 附件"
    assert "报警日报" in mailer.subject_for(mailer.build_report(store, offset=1), cfg)


def test_send_starttls_path():
    tmp = tempfile.mkdtemp()
    store = _mk(tmp, {1: 1})
    cfg = _cfg(tmp, mail_ssl=False, mail_starttls=True, mail_port=587)
    res = mailer.send_daily_report(cfg, store)
    assert res["ok"] is True, res
    s = FakeSMTP.instances[0]
    assert s.starttls_called is True and s.port == 587


def test_skip_if_empty_and_no_recipient():
    tmp = tempfile.mkdtemp()
    store = storage_mod.Storage(os.path.join(tmp, "v.db"))
    cfg = _cfg(tmp, mail_skip_if_empty=True)
    res = mailer.send_daily_report(cfg, store)
    assert res["ok"] is True and res.get("skipped") is True
    assert not FakeSMTP.instances, "跳过时不应连接 SMTP"

    cfg2 = _cfg(tmp, mail_to="", mail_user="")
    res2 = mailer.send_daily_report(cfg2, _mk(tmp, {1: 1}))
    assert res2["ok"] is False and "收件人" in res2["msg"]


def test_dry_run_does_not_connect():
    tmp = tempfile.mkdtemp()
    store = _mk(tmp, {1: 1})
    res = mailer.send_daily_report(_cfg(tmp), store, dry_run=True)
    assert res["ok"] is True and not FakeSMTP.instances


def test_send_failure_is_reported_not_raised(monkeypatch):
    tmp = tempfile.mkdtemp()

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", boom)
    res = mailer.send_daily_report(_cfg(tmp), _mk(tmp, {1: 1}))
    assert res["ok"] is False and "发送失败" in res["msg"]


def test_send_specific_day():
    tmp = tempfile.mkdtemp()
    store = _mk(tmp, {3: 2})
    day = (datetime.datetime.now().date() - datetime.timedelta(days=3)).isoformat()
    res = mailer.send_daily_report(_cfg(tmp), store, day=day)
    assert res["ok"] is True and res["day"] == day and res["count"] == 2


# ---------------- 到点判断 ----------------
def test_due_now_and_public_config():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp, mail_send_time="09:00")
    now = datetime.datetime(2026, 9, 27, 8, 59)
    assert mailer.due_now(cfg, now) == (False, "2026-09-27")
    assert mailer.due_now(cfg, datetime.datetime(2026, 9, 27, 9, 0))[0] is True
    assert mailer.due_now(cfg, datetime.datetime(2026, 9, 27, 23, 30))[0] is True  # 启动晚也补发
    assert mailer.parse_hm("bad") == (9, 0)

    pub = mailer.public_config(cfg)
    assert pub["mail_password"] == "●●●●●●" and pub["mail_password_set"] is True
    assert "qq" in pub["presets"]


# ---------------- 服务端接口 ----------------
def test_mail_endpoints(monkeypatch):
    pytest.importorskip("aiohttp")
    from volmon import alerting as A
    from volmon import server as server_mod
    from volmon import sim as sim_mod

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(server_mod, "ROOT", tmp)
    cfg = _cfg(tmp)
    cfg.set("alerts_csv_path", os.path.join(tmp, "alerts.csv"))
    store = _mk(tmp, {1: 2})
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    srv = server_mod.MonitorServer(sim_mod.SimEngine(), store, cfg, alm)

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(srv.app))
        await client.start_server()
        try:
            r = await client.get("/api/mail")
            j = await r.json()
            assert j["ok"] is True and j["mail_password"] == "●●●●●●"
            assert j["mail_to"] == "boss@example.com, ops@example.com"

            # 预览：返回渲染好的 HTML，且不连接 SMTP
            r = await client.post("/api/mail/send", json={"dry_run": True})
            j = await r.json()
            assert j["ok"] is True and j["count"] == 2 and "报警日报" in j["html"]
            assert not FakeSMTP.instances

            # 真发一次
            r = await client.post("/api/mail/send", json={})
            j = await r.json()
            assert j["ok"] is True and j["count"] == 2
            assert len(FakeSMTP.instances) == 1
            assert "已发送" in cfg.get("mail_last_result")

            # 配置项可通过 /api/config 保存（白名单里要有 mail_*）
            r = await client.post("/api/config", json={"mail_send_time": "21:30"})
            j = await r.json()
            assert j["applied"]["mail_send_time"] == "21:30"
        finally:
            await client.close()

    asyncio.run(_run())


def test_daily_scheduler_sends_once_per_day(monkeypatch):
    """到点后只发一次：第二次检查因 mail_last_sent_day 已是今天而跳过。"""
    pytest.importorskip("aiohttp")
    from volmon import alerting as A
    from volmon import server as server_mod
    from volmon import sim as sim_mod

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(server_mod, "ROOT", tmp)
    cfg = _cfg(tmp, mail_send_time="00:00")   # 一定"已到点"
    store = _mk(tmp, {1: 1})
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    srv = server_mod.MonitorServer(sim_mod.SimEngine(), store, cfg, alm)

    srv._maybe_send_daily_mail()
    assert len(FakeSMTP.instances) == 1
    assert cfg.get("mail_last_sent_day") == datetime.datetime.now().strftime("%Y-%m-%d")
    srv._maybe_send_daily_mail()
    srv._maybe_send_daily_mail()
    assert len(FakeSMTP.instances) == 1, "同一天不应重复发送"

    # 关掉开关后不再发
    cfg.set("mail_enabled", False)
    cfg.set("mail_last_sent_day", "")
    srv._maybe_send_daily_mail()
    assert len(FakeSMTP.instances) == 1


def test_daily_scheduler_retries_at_most_3_times(monkeypatch):
    tmp = tempfile.mkdtemp()
    from volmon import alerting as A
    from volmon import server as server_mod
    from volmon import sim as sim_mod

    monkeypatch.setattr(server_mod, "ROOT", tmp)
    cfg = _cfg(tmp, mail_send_time="00:00", mail_host="")   # 缺 host -> 必失败
    store = _mk(tmp, {1: 1})
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    srv = server_mod.MonitorServer(sim_mod.SimEngine(), store, cfg, alm)
    for _ in range(5):
        srv._maybe_send_daily_mail()
    assert srv._mail_fail_count == 3, "当天最多重试 3 次"
    assert cfg.get("mail_last_sent_day") == "", "失败不应记为已发送"
