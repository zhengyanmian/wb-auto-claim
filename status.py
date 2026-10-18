# -*- coding: utf-8 -*-
"""
查看 WorkBuddy 每日领积分的执行状态
----------------------------------
双击 status.bat 就能看到：今天执行了没有、什么时候执行的、结果如何、下一次什么时候跑。
  python status.py            # 完整报告
  python status.py --brief    # 一句话摘要（给定时任务汇报用）
  python status.py --days 14  # 看最近 14 天
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
CONFIG_PATH = os.path.join(BASE, "config.json")
HISTORY_PATH = os.path.join(BASE, "history.jsonl")
STATUS_TXT = os.path.join(BASE, "status.txt")
TASK_NAME = "WorkBuddy-AutoClaim"

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 结果 -> 中文标签
LABEL = {
    "success": "成功领取",
    "already_claimed": "已是「今日已领」，跳过",
    "card_not_found": "找不到积分卡片",
    "no_card": "卡片不在（收起了 / 未加载）",
    "window_not_found": "WorkBuddy 窗口没开也起不来",
    "uncertain": "点了但状态没变，需人工确认",
    "locked": "锁屏，点不了",
    "dry_run": "试运行（未点击）",
    "probe": "仅定位（未点击）",
    "error": "脚本异常中断",
}
FAIL_HINT = {
    "card_not_found": "积分卡片不可见（卡片被关掉 / 被别的窗口盖住 / 活动结束 / 页面没加载完）",
    "no_card": "左下角卡片区是空的：你领完顺手把它收起了就会这样（属正常），"
               "否则是卡片被关掉 / 活动结束 / 页面没加载完；展开卡片后下一次触发会补领",
    "window_not_found": "WorkBuddy 没启动且自动启动失败",
    "uncertain": "点击后按钮没变成「今日已领」",
    "locked": "当时电脑处于锁屏状态，点不了；解锁后会自动补领当天积分",
    "error": "脚本抛了异常，细节见 logs 目录里的日志",
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_history():
    recs = []
    if not os.path.exists(HISTORY_PATH):
        return recs
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    recs.sort(key=lambda r: r.get("ts", ""))
    return recs


def task_next_run():
    """从计划任务里取「下次运行时间」；任务不存在返回 "missing"，取不到返回 "unknown"。

    优先用任务计划 COM 接口：2026-10 起本机安全策略把 schtasks.exe 拉黑了，
    只靠 schtasks 的话会永远拿到 unknown —— 那状态页就既报不出「未注册」也报不出下次运行时间。"""
    try:
        import install_task as it
        t = it.service().GetFolder("\\").GetTask(TASK_NAME)
        nxt = t.NextRunTime
        if not nxt:
            return "unknown"
        dt = datetime(1899, 12, 30) + timedelta(days=float(nxt))   # OLE 自动化日期
        return dt.strftime("%m-%d %H:%M")
    except Exception as e:
        code = getattr(e, "args", [None])[0] if getattr(e, "args", None) else None
        if code in (-2147024894, 0x80070002) or "找不到" in str(e) or "cannot find" in str(e).lower():
            return "missing"
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"],
                           capture_output=True)
        if r.returncode != 0:
            return "missing"
        out = r.stdout
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            text = out.decode("gbk", errors="ignore")
        now = datetime.now()
        for m in re.finditer(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})", text):
            y, mo, d, h, mi, s = (int(x) for x in m.groups())
            dt = datetime(y, mo, d, h, mi, s)
            if dt > now:
                return dt.strftime("%m-%d %H:%M")
        return "unknown"
    except Exception:
        return "unknown"


def next_slot(times, now=None):
    """根据配置的触发时间算出下一次时点"""
    now = now or datetime.now()
    for t in sorted(times):
        try:
            h, mi = (int(x) for x in t.split(":"))
        except Exception:
            continue
        cand = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand > now:
            return cand
    first = sorted(times)[0]
    h, mi = (int(x) for x in first.split(":"))
    return (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)


def human_delta(dt, now=None):
    now = now or datetime.now()
    secs = int((dt - now).total_seconds())
    if secs < 0:
        return "已过"
    h, m = divmod(secs // 60, 60)
    if h:
        return "%d 小时 %d 分后" % (h, m)
    return "%d 分钟后" % m


def day_summary(date_str, recs):
    """返回某一天的 (状态标记, 一句话, 明细行列表)"""
    day_recs = [r for r in recs if r.get("date") == date_str]
    lines = []
    for r in day_recs:
        lbl = LABEL.get(r.get("result"), r.get("result", "?"))
        lines.append("    %s  %s" % (r.get("time", "??:??:??"), lbl))

    success = next((r for r in day_recs if r.get("result") == "success"), None)
    tries = len(day_recs)

    if success:
        return ("已执行",
                "%s 成功领取（当天共 %d 次检查）" % (success.get("time", "")[:5], tries),
                lines)
    if not day_recs:
        return ("未执行", "今天还没有任何执行记录", lines)

    # 只要当天确认过「已经是今日已领」（不管前面失败几次），就算达成 ——
    # 否则你自己手动领过之后，状态页还会一直显示「未成功」，误导你去查错方向
    claimed = next((r for r in day_recs if r.get("result") == "already_claimed"), None)
    if claimed:
        return ("已达成",
                "%s 检查时已经是「今日已领」（你自己领的 / 之前已领成功）" % claimed.get("time", "")[:5],
                lines)

    # 全都是锁屏导致没跑成：单独说清楚，别让人以为是脚本坏了
    if all(r.get("result") == "locked" for r in day_recs):
        return ("未成功",
                "%d 次触发时电脑都处于锁屏状态，点不了；解锁后会自动补领" % tries,
                lines)

    # 卡片一直不在：最常见的原因是「你自己领完顺手把卡片收起了」，
    # 这时候界面没有任何可读信号，别报成「未成功」把人往查错的方向引。
    if all(r.get("result") in ("no_card", "card_not_found") for r in day_recs):
        return ("待确认",
                "%d 次触发时左下角都没有卡片。若是你领完收起了卡片，属正常；"
                "若当天还没领，请展开卡片，下一次触发会自动补领" % tries,
                lines)

    last = day_recs[-1]
    hint = FAIL_HINT.get(last.get("result"), "")
    return ("未成功",
            "已尝试 %d 次都没成功，最近一次 %s：%s%s" % (
                tries, last.get("time", "")[:5],
                LABEL.get(last.get("result"), last.get("result")),
                "（%s）" % hint if hint else ""),
            lines)


def build_report(days=7):
    cfg = load_config()
    times = cfg.get("schedule_times", ["09:00", "12:30", "16:30"])
    recs = load_history()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    out = []
    out.append("=" * 52)
    out.append(" WorkBuddy 每日领积分 · 执行状态")
    out.append(" 更新时间 %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
    out.append("=" * 52)

    mark, desc, lines = day_summary(today, recs)
    icon = {"已执行": "【已执行】", "已达成": "【已达成】",
            "未执行": "【未执行】", "未成功": "【未成功】",
            "待确认": "【待确认】"}[mark]
    out.append("")
    out.append("今天（%s %s）" % (today, WEEK[now.weekday()]))
    out.append("  %s %s" % (icon, desc))
    if lines:
        out.append("  执行记录：")
        out.extend(lines)

    if mark in ("未执行", "未成功", "待确认"):
        nxt = next_slot(times, now)
        out.append("  下一次自动尝试：%s（%s）" % (nxt.strftime("%m-%d %H:%M"), human_delta(nxt, now)))
    else:
        out.append("  今天已经完成，后面的计划触发只会做一次确认检查。")

    out.append("")
    out.append("最近 %d 天" % days)
    ok_days = 0
    counted = 0
    first_date = min((r.get("date", "") for r in recs), default=today)
    for i in range(days):
        d = now - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        if ds < first_date:
            out.append("  %s %s  （工具未启用）" % (ds[5:], WEEK[d.weekday()]))
            continue
        counted += 1
        m, desc_d, _ = day_summary(ds, recs)
        tag = {"已执行": "已执行", "已达成": "已达成",
               "未执行": "未执行", "未成功": "未成功", "待确认": "待确认"}[m]
        if m in ("已执行", "已达成"):
            ok_days += 1
        first_time = ""
        day_recs = [r for r in recs if r.get("date") == ds]
        if day_recs:
            first_time = day_recs[0].get("time", "")[:5]
        out.append("  %s %s  %-6s %s" % (ds[5:], WEEK[d.weekday()], tag,
                                         ("于 " + first_time) if first_time else ""))
    out.append("  合计：%d / %d 天完成" % (ok_days, counted))

    out.append("")
    out.append("计划任务")
    exists = task_next_run()
    if exists == "missing":
        out.append("  【未注册】任务 %s 不存在 —— 双击 install_task.bat 重新注册" % TASK_NAME)
    elif exists in ("unknown",):
        out.append("  任务 %s 已注册；触发时间 %s" % (TASK_NAME, " / ".join(times)))
    else:
        out.append("  任务 %s 运行中；下次运行 %s；触发时间 %s"
                   % (TASK_NAME, exists, " / ".join(times)))
    if cfg.get("extra_triggers"):
        out.append("  另外还有：%s（锁屏时点不了，解锁后会补领）" % "、".join(cfg["extra_triggers"]))

    out.append("")
    out.append("排查提示：")
    out.append("  「锁屏，点不了」→ 正常：锁屏时谁也点不动。解锁后会自动补领（装了补领触发的话）")
    out.append("  「找不到积分卡片或按钮」→ 要么卡片被别的窗口盖住了（脚本会自己抬前台重试），")
    out.append("                             要么卡片被关掉 / 活动结束了（活动期结束是正常现象）")
    out.append("  「卡片在但没识别到可领按钮」→ 界面可能又改版，跑 selftest.py 看识别结果，")
    out.append("                             并看 captures/card_region.png 确认按钮长什么样")
    out.append("  识别逻辑不依赖任何模板，所以一般不需要重新「生成模板」")
    out.append("")
    return "\n".join(out)


def build_brief():
    cfg = load_config()
    times = cfg.get("schedule_times", ["09:00", "12:30", "16:30"])
    recs = load_history()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    mark, desc, _ = day_summary(today, recs)
    if mark in ("已执行", "已达成"):
        return "%s WorkBuddy 自动领积分：%s。" % (today, desc)
    nxt = next_slot(times, now)
    return "%s WorkBuddy 自动领积分：尚未完成 —— %s；下一次自动尝试 %s。" % (
        today, desc, nxt.strftime("%H:%M"))


def write_status_file():
    try:
        with open(STATUS_TXT, "w", encoding="utf-8") as f:
            f.write(build_report())
    except Exception:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true", help="一句话摘要")
    ap.add_argument("--days", type=int, default=7, help="最近多少天")
    ap.add_argument("--no-write", action="store_true", help="不刷新 status.txt")
    a = ap.parse_args()
    if a.brief:
        print(build_brief())
    else:
        txt = build_report(a.days)
        print(txt)
        if not a.no_write:
            write_status_file()
    sys.exit(0)
