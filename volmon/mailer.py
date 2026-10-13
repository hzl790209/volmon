"""每日报警日志邮件推送（只用标准库 smtplib，不新增依赖）。

能力：
  - 生成「某一天的报警日报」：条数、峰值、总超标时长、按时段/阈值分布、明细表
  - 正文同时提供 HTML 与纯文本；可选附带当天的告警 CSV 附件
  - SMTP / SMTP_SSL / STARTTLS 都支持，配置全在页面「每日报警邮件」面板里填
  - 服务后台每天到点自动发（可配置几点、发昨天还是今天），也能手动立即发/补发

安全提示：邮箱密码请填**授权码**（QQ/163/Gmail 都需在邮箱设置里开启 SMTP 后生成），
不要填登录密码。
"""
import datetime
import logging
import os
import smtplib
import tempfile
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, parseaddr

from volmon import __version__

logger = logging.getLogger("volmon.mailer")

# 页面上可直接改的配置项（内部状态键 mail_last_* 不在此列）
MAIL_KEYS = [
    "mail_enabled", "mail_host", "mail_port", "mail_ssl", "mail_starttls",
    "mail_user", "mail_password", "mail_from", "mail_to",
    "mail_send_time", "mail_day_offset", "mail_attach_csv", "mail_skip_if_empty",
]
SECRET_KEYS = {"mail_password"}

# 常用邮箱预设（页面「一键填入」用）
PRESETS = {
    "qq":   {"mail_host": "smtp.qq.com",   "mail_port": 465, "mail_ssl": True,  "mail_starttls": False},
    "163":  {"mail_host": "smtp.163.com",  "mail_port": 465, "mail_ssl": True,  "mail_starttls": False},
    "gmail": {"mail_host": "smtp.gmail.com", "mail_port": 587, "mail_ssl": False, "mail_starttls": True},
    "outlook": {"mail_host": "smtp.office365.com", "mail_port": 587, "mail_ssl": False, "mail_starttls": True},
    "qqex": {"mail_host": "smtp.exmail.qq.com", "mail_port": 465, "mail_ssl": True, "mail_starttls": False},
}


def parse_hm(text, default=(9, 0)):
    """'09:00' -> (9, 0)；解析失败返回 default。"""
    try:
        h, m = str(text or "").strip().split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return default


def day_bounds(day=None, offset=1):
    """返回 (day_str, start_ms, end_ms)。offset=1 表示昨天，0 表示今天（截至当前）。"""
    today = datetime.datetime.now().date()
    if day:
        try:
            d = datetime.date.fromisoformat(str(day))
        except ValueError:
            d = today
    else:
        d = today - datetime.timedelta(days=int(offset or 0))
    start = datetime.datetime.combine(d, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    if int(offset or 0) == 0 and not day:      # 今天：只到当前时刻
        end = datetime.datetime.now()
    return d.isoformat(), int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def split_addrs(text):
    """'a@b.com, c@d.com' -> ['a@b.com', 'c@d.com']"""
    out, seen = [], set()
    for part in str(text or "").replace(";", ",").split(","):
        addr = parseaddr(part.strip())[1] or part.strip()
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def build_report(storage, day=None, offset=1, top=50):
    """汇总某一天的报警情况，返回报表 dict。"""
    day_str, start_ms, end_ms = day_bounds(day, offset)
    rows = storage.get_alerts_range(start_ms, end_ms)

    total_ms = sum((r["end_ts"] or r["start_ts"]) - r["start_ts"] for r in rows)
    peak_row = max(rows, key=lambda r: r["peak_db"]) if rows else None
    longest = max(rows, key=lambda r: (r["duration_s"] or 0)) if rows else None

    by_period, by_threshold = {}, {}
    alarm_on = 0
    for r in rows:
        key = r["period"] if (r["schedule_applied"] and r["period"]) else "全局"
        by_period[key] = by_period.get(key, 0) + 1
        t = f"{float(r['threshold_db']):.0f}"
        by_threshold[t] = by_threshold.get(t, 0) + 1
        if r["alarm_enabled"]:
            alarm_on += 1

    return {
        "day": day_str,
        "range": f"{_fmt_ms(start_ms)} ~ {_fmt_ms(end_ms)}",
        "count": len(rows),
        "peak_db": round(peak_row["peak_db"], 2) if peak_row else None,
        "peak_time": _fmt_ms(peak_row["start_ts"]) if peak_row else "",
        "avg_peak_db": round(sum(r["peak_db"] for r in rows) / len(rows), 2) if rows else None,
        "total_over_sec": round(total_ms / 1000.0, 1),
        "longest_sec": (longest["duration_s"] if longest else None),
        "longest_time": _fmt_ms(longest["start_ts"]) if longest else "",
        "by_period": by_period,
        "by_threshold": by_threshold,
        "alarm_on_count": alarm_on,
        "rows": rows[:top],
        "row_total": len(rows),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": __version__,
    }


def _fmt_ms(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def subject_for(rep, cfg=None):
    prefix = (cfg.get("mail_subject_prefix") if cfg else None) or "[音量监控]"
    if rep["count"]:
        return f"{prefix} {rep['day']} 报警日报：{rep['count']} 次超量，峰值 {rep['peak_db']:.1f} dB"
    return f"{prefix} {rep['day']} 报警日报：无超量告警"


def render_text(rep):
    lines = [
        f"音量监控 · 报警日报（{rep['day']}）",
        f"统计区间：{rep['range']}",
        f"生成时间：{rep['generated_at']}　软件版本 v{rep['version']}",
        "",
    ]
    if not rep["count"]:
        lines.append("当日没有检测到超量告警。")
        return "\n".join(lines)

    lines += [
        f"超量次数：{rep['count']} 次",
        f"峰值：{rep['peak_db']:.1f} dB（{rep['peak_time']}）",
        f"平均峰值：{rep['avg_peak_db']:.1f} dB",
        f"累计超标时长：{rep['total_over_sec']:.1f} 秒",
        f"最长一次：{rep['longest_sec']} 秒（{rep['longest_time']}）",
        f"其中开启声音报警：{rep['alarm_on_count']} 次",
        "时段分布：" + "，".join(f"{k} {v} 次" for k, v in rep["by_period"].items()),
        "阈值分布：" + "，".join(f"{k} dB {v} 次" for k, v in rep["by_threshold"].items()),
        "",
        "明细（最多 50 条）：",
        "时间　　　　　　　　峰值dB　阈值dB　持续s　时段/策略　声音报警",
    ]
    for r in rep["rows"]:
        period = r["period"] if (r["schedule_applied"] and r["period"]) else "全局"
        lines.append(
            f"{_fmt_ms(r['start_ts'])}　{float(r['peak_db']):6.1f}　{float(r['threshold_db']):6.1f}"
            f"　{(r['duration_s'] or 0):5.1f}　{period}　{'开' if r['alarm_enabled'] else '关'}"
        )
    return "\n".join(lines)


def render_html(rep):
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    if not rep["count"]:
        body = ("<p style='font-size:15px;'>当日<b>没有</b>检测到超量告警。</p>")
    else:
        stats = [
            ("超量次数", f"{rep['count']} 次"),
            ("峰值", f"{rep['peak_db']:.1f} dB　<small>{esc(rep['peak_time'])}</small>"),
            ("平均峰值", f"{rep['avg_peak_db']:.1f} dB"),
            ("累计超标时长", f"{rep['total_over_sec']:.1f} 秒"),
            ("最长一次", f"{rep['longest_sec']} 秒　<small>{esc(rep['longest_time'])}</small>"),
            ("开启声音报警", f"{rep['alarm_on_count']} 次"),
            ("时段分布", "，".join(f"{esc(k)} {v} 次" for k, v in rep["by_period"].items())),
            ("阈值分布", "，".join(f"{esc(k)} dB {v} 次" for k, v in rep["by_threshold"].items())),
        ]
        cards = "".join(
            f"<td style='padding:8px 14px;border:1px solid #e5e7eb;'>"
            f"<div style='color:#6b7280;font-size:12px;'>{esc(k)}</div>"
            f"<div style='font-size:16px;font-weight:700;'>{v}</div></td>"
            for k, v in stats
        )
        trs = "".join(
            "<tr>"
            f"<td style='padding:5px 8px;border-bottom:1px solid #e5e7eb;'>{esc(_fmt_ms(r['start_ts']))}</td>"
            f"<td style='padding:5px 8px;border-bottom:1px solid #e5e7eb;color:#dc2626;font-weight:700;'>{float(r['peak_db']):.1f}</td>"
            f"<td style='padding:5px 8px;border-bottom:1px solid #e5e7eb;'>{float(r['threshold_db']):.1f}</td>"
            f"<td style='padding:5px 8px;border-bottom:1px solid #e5e7eb;'>{(r['duration_s'] or 0):.1f}</td>"
            f"<td style='padding:5px 8px;border-bottom:1px solid #e5e7eb;'>{esc(r['period'] if (r['schedule_applied'] and r['period']) else '全局')}</td>"
            f"<td style='padding:5px 8px;border-bottom:1px solid #e5e7eb;'>{'开' if r['alarm_enabled'] else '关'}</td>"
            "</tr>"
            for r in rep["rows"]
        )
        body = (
            f"<table style='border-collapse:collapse;font-size:13px;margin-bottom:14px;'>{cards}</table>"
            f"<h3 style='font-size:14px;margin:0 0 6px;'>明细（{rep['row_total']} 条"
            + (f"，显示前 {len(rep['rows'])} 条" if rep["row_total"] > len(rep["rows"]) else "")
            + "）</h3>"
            "<table style='border-collapse:collapse;font-size:12px;width:100%;'>"
            "<tr style='background:#f3f4f6;'>"
            "<th style='padding:6px 8px;text-align:left;'>开始时间</th>"
            "<th style='padding:6px 8px;text-align:left;'>峰值 dB</th>"
            "<th style='padding:6px 8px;text-align:left;'>阈值 dB</th>"
            "<th style='padding:6px 8px;text-align:left;'>持续 s</th>"
            "<th style='padding:6px 8px;text-align:left;'>时段/策略</th>"
            "<th style='padding:6px 8px;text-align:left;'>声音报警</th>"
            f"</tr>{trs}</table>"
        )

    return (
        "<div style='font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;"
        "color:#1f2933;font-size:14px;line-height:1.6;'>"
        f"<h2 style='font-size:18px;margin:0 0 4px;'>🔊 音量监控 · 报警日报（{esc(rep['day'])}）</h2>"
        f"<div style='color:#6b7280;font-size:12px;margin-bottom:14px;'>"
        f"统计区间 {esc(rep['range'])}　·　生成于 {esc(rep['generated_at'])}　·　v{esc(rep['version'])}</div>"
        f"{body}"
        "<hr style='border:0;border-top:1px solid #e5e7eb;margin:16px 0;'/>"
        "<div style='color:#9ca3af;font-size:12px;'>本邮件由 volmon 音量监控软件自动发送；"
        "修改推送设置请打开软件 Web 页面的「每日报警邮件」面板。</div>"
        "</div>"
    )


def _attachment_for(storage, cfg, rep, day_str, start_ms, end_ms):
    """准备 CSV 附件：优先用已归档的当天 CSV，没有则即时从库里导出一份。"""
    if not cfg.get("mail_attach_csv"):
        return None
    csv_path = cfg.get("alerts_csv_path")
    try:
        from volmon import logrot

        archived = logrot.archive_path(csv_path, day_str) if csv_path else None
        if archived and os.path.exists(archived) and os.path.getsize(archived) > 0:
            return (f"alerts_{day_str}.csv", archived)
    except Exception:
        pass
    if not rep["count"]:
        return None
    try:
        tmp = os.path.join(tempfile.gettempdir(), f"volmon_alerts_{day_str}.csv")
        storage.export_alerts_range_csv(tmp, start_ms, end_ms)
        return (f"alerts_{day_str}.csv", tmp)
    except Exception as e:
        logger.warning("生成日报 CSV 附件失败: %s", e)
        return None


def build_message(cfg, rep, attachments=None):
    """构造 MIME 邮件对象（HTML + 纯文本 + 附件）。"""
    to_list = split_addrs(cfg.get("mail_to")) or split_addrs(cfg.get("mail_user"))
    sender = cfg.get("mail_from") or cfg.get("mail_user") or "volmon@localhost"
    text = render_text(rep)
    html = render_html(rep)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = Header(subject_for(rep, cfg), "utf-8")
    msg["From"] = formataddr(("音量监控 volmon", sender))
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    for name, path in (attachments or []):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), "octet-stream")
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", name))
            msg.attach(part)
        except Exception as e:
            logger.warning("附加 %s 失败: %s", path, e)
    return msg, to_list


def send_daily_report(cfg, storage, day=None, dry_run=False):
    """生成并发送某一天的报警日报。

    day: 指定日期（YYYY-MM-DD）；None 时用配置的 mail_day_offset（1=昨天）。
    dry_run: 只生成不发送，用于预览/自检。
    返回 {"ok","msg","day","count","to","subject"}。
    """
    offset = int(cfg.get("mail_day_offset") or 0)
    day_str, start_ms, end_ms = day_bounds(day, 0 if day else offset)
    rep = build_report(storage, day=day_str, offset=0)
    # 指定了具体日期时区间要按整天算
    if day:
        rep["range"] = f"{_fmt_ms(start_ms)} ~ {_fmt_ms(end_ms)}"
    subject = subject_for(rep, cfg)
    to_list = split_addrs(cfg.get("mail_to")) or split_addrs(cfg.get("mail_user"))
    out = {"ok": False, "msg": "", "day": day_str, "count": rep["count"],
           "to": to_list, "subject": subject}

    if not to_list:
        out["msg"] = "未配置收件人（mail_to / mail_user）"
        return out
    if dry_run:
        out["ok"] = True
        out["msg"] = f"预览生成成功（未发送）：{rep['count']} 条告警"
        return out
    if rep["count"] == 0 and cfg.get("mail_skip_if_empty"):
        out["ok"] = True
        out["msg"] = "当日无超量告警，按设置跳过发送"
        out["skipped"] = True
        return out

    att = _attachment_for(storage, cfg, rep, day_str, start_ms, end_ms)
    msg, to_list = build_message(cfg, rep, [att] if att else None)

    host = (cfg.get("mail_host") or "").strip()
    port = int(cfg.get("mail_port") or 0)
    if not host or not port:
        out["msg"] = "未配置 SMTP 服务器（mail_host / mail_port）"
        return out

    try:
        timeout = float(cfg.get("mail_timeout_sec") or 30)
        if cfg.get("mail_ssl"):
            s = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            s = smtplib.SMTP(host, port, timeout=timeout)
            if cfg.get("mail_starttls"):
                s.starttls()
        try:
            user = (cfg.get("mail_user") or "").strip()
            pwd = cfg.get("mail_password") or ""
            if user and pwd:
                s.login(user, pwd)
            s.sendmail(cfg.get("mail_from") or user or host, to_list, msg.as_string())
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except Exception as e:
        out["msg"] = f"发送失败：{type(e).__name__}: {e}"
        logger.warning("报警日报邮件发送失败: %s", out["msg"])
        return out

    out["ok"] = True
    out["msg"] = (f"已发送至 {', '.join(to_list)}：{rep['count']} 条告警"
                  + (f"，附件 {os.path.basename(att[1])}" if att else ""))
    out["sent_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("报警日报邮件已发送：%s", out["msg"])
    return out


def public_config(cfg):
    """给页面用的配置（密码脱敏）。"""
    data = {k: cfg.get(k) for k in MAIL_KEYS}
    data["mail_password_set"] = bool(cfg.get("mail_password"))
    data["mail_password"] = "●●●●●●" if cfg.get("mail_password") else ""
    data["mail_last_sent_day"] = cfg.get("mail_last_sent_day") or ""
    data["mail_last_result"] = cfg.get("mail_last_result") or ""
    data["presets"] = PRESETS
    return data


def preview(cfg, storage, day=None):
    """生成报表预览（不发送），返回渲染好的 HTML / 纯文本，供页面直接展示。"""
    offset = int(cfg.get("mail_day_offset") or 0)
    day_str, start_ms, end_ms = day_bounds(day, 0 if day else offset)
    rep = build_report(storage, day=day_str, offset=0)
    if day:
        rep["range"] = f"{_fmt_ms(start_ms)} ~ {_fmt_ms(end_ms)}"
    return {
        "ok": True, "day": day_str, "count": rep["count"],
        "subject": subject_for(rep, cfg),
        "html": render_html(rep), "text": render_text(rep),
    }


def due_now(cfg, now=None):
    """当前是否到了当天该发送的时刻（含"启动晚了补发"）。

    返回 (是否该发, 今天的日期字符串)。
    """
    now = now or datetime.datetime.now()
    h, m = parse_hm(cfg.get("mail_send_time"))
    if (now.hour, now.minute) < (h, m):
        return False, now.strftime("%Y-%m-%d")
    return True, now.strftime("%Y-%m-%d")
