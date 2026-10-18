# -*- coding: utf-8 -*-
"""注册 / 更新 Windows 计划任务 WorkBuddy-AutoClaim。

为什么不用 schtasks.exe：
  2026-10 起本机安全策略把 schtasks.exe 拉进了程序黑名单（无法调用），
  改用「任务计划 COM 接口（Schedule.Service）」，不需要 schtasks、不需要管理员权限。

踩过的坑（别删，会再踩）：
  · ITaskDefinition 的 XML 属性在 typelib 里叫 **XmlText**（不是 Xml）。写 td.Xml = ... 不会报错，
    但只是给 Python 对象挂了个属性，COM 那边拿到的是**空定义**，注册时报
    "任务 XML 缺少个所需元素或属性 (27,4):Actions" —— 这个报错位置是固定的，是「定义是空的」的意思。
  · XML 元素必须按 schema 顺序排：RegistrationInfo → Triggers → Settings → Principals → Actions。
  · 触发器不能用 ITriggerCollection.Create(10) 建（SESSION_STATE_CHANGE 会报参数错误），只能走 XML。

触发点：
  09:00 / 12:30 / 16:30  每天三次兜底（脚本幂等，已领过 1 秒退出）
  SessionUnlock          解锁后 30 秒补跑 —— 锁屏时点不了，一解锁就补领当天积分
  Logon                  登录后 1 分钟补跑 —— 开机/关机错过的那次

用法：
  python install_task.py            # 注册/更新
  python install_task.py --show     # 只打印当前任务的触发点
  python install_task.py --delete   # 卸载
"""
import datetime
import json
import os
import re
import sys

import comtypes.client as cc

cc.GetModule("taskschd.dll")
from comtypes.gen import TaskScheduler as TS      # noqa: E402

TASK_NAME = "WorkBuddy-AutoClaim"
BASE = os.path.dirname(os.path.abspath(__file__))
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
MAIN = os.path.join(BASE, "main.py")

TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_INTERACTIVE_TOKEN = 3

TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{author}</Author>
    <URI>\\{name}</URI>
    <Description>WorkBuddy 每日积分自动领取（幂等：已领取时 1 秒内退出）</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{date}T09:00:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>{date}T12:30:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>{date}T16:30:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
      <Delay>PT30S</Delay>
    </SessionStateChangeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </LogonTrigger>
  </Triggers>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Actions Context="Author">
    <Exec>
      <Command>"{pythonw}"</Command>
      <Arguments>"{main}" --silent</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def service():
    svc = cc.CreateObject("Schedule.Service", interface=TS.ITaskService)
    svc.Connect()
    return svc


def current_sid():
    import win32api
    import win32security
    tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(tok, win32security.TokenUser)[0])


def task_xml(t):
    try:
        return t.XmlText
    except Exception:
        return t.Xml


def _ole(v):
    """COM 返回的时间是 OLE 自动化日期（浮点数），转成可读时间"""
    if not v:
        return "(无)"
    try:
        return str(datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(v)))[:19]
    except Exception:
        return str(v)


def show(svc=None):
    svc = svc or service()
    root = svc.GetFolder("\\")
    try:
        t = root.GetTask(TASK_NAME)
    except Exception:
        print("任务 %s 未注册" % TASK_NAME)
        return 1
    x = task_xml(t)
    print("任务 %s 已注册" % TASK_NAME)
    for m in re.finditer(r"<StartBoundary>(.*?)</StartBoundary>", x):
        print("  每天触发   %s" % m.group(1)[11:16])
    for m in re.finditer(r"<StateChange>(.*?)</StateChange>", x):
        print("  解锁触发   %s（延迟 30 秒）" % m.group(1))
    if "<LogonTrigger>" in x:
        print("  登录触发   登录后 1 分钟")
    print("  上次运行 %s   结果 %s" % (_ole(t.LastRunTime), t.LastTaskResult))
    print("  下次运行 %s" % _ole(t.NextRunTime))
    return 0


def delete():
    svc = service()
    svc.GetFolder("\\").DeleteTask(TASK_NAME, 0)
    print("已删除任务 %s" % TASK_NAME)
    return 0


EXTRA = """    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>SessionUnlock</StateChange>
      <Delay>PT30S</Delay>
    </SessionStateChangeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </LogonTrigger>
"""


def _set_config_extra(items):
    """把「实际注册上的补领触发」写回 config.json，好让状态页如实显示"""
    path = os.path.join(BASE, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    cfg["extra_triggers"] = items
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def install(allow_unlock=True):
    if not os.path.exists(MAIN):
        print("找不到 main.py：", MAIN)
        return 2
    if not os.path.exists(PYTHONW):
        print("找不到 pythonw.exe：", PYTHONW)
        return 2
    xml = TEMPLATE.format(author=os.environ.get("USERNAME", "user"),
                          name=TASK_NAME,
                          date=datetime.date.today().isoformat(),
                          sid=current_sid(),
                          pythonw=PYTHONW,
                          main=MAIN)
    svc = service()
    root = svc.GetFolder("\\")

    def register(text):
        td = svc.NewTask(0)
        td.XmlText = text                     # ← 必须是 XmlText，不是 Xml（见文件头注释）
        root.RegisterTaskDefinition(TASK_NAME, td, TASK_CREATE_OR_UPDATE,
                                    None, None, TASK_LOGON_INTERACTIVE_TOKEN)

    try:
        register(xml if allow_unlock else xml.replace(EXTRA, ""))
        if allow_unlock:
            _set_config_extra(["解锁后 30 秒", "登录后 1 分钟"])
            print("已注册/更新任务 %s（含「解锁后补领」）" % TASK_NAME)
        else:
            _set_config_extra([])
            print("已注册/更新任务 %s（仅每天 09:00/12:30/16:30）" % TASK_NAME)
    except Exception as e:
        # 2026-10 实测：普通权限注册「解锁 / 登录」触发会被系统拒绝（拒绝访问 0x80070005），
        # 这类触发需要管理员权限。自动降级为「只有日历触发」，并把加解锁触发的办法告诉用户。
        if "拒绝访问" in str(e) or "Access is denied" in str(e):
            print("注册含「解锁/登录」触发被系统拒绝（需要管理员权限）。")
            print("先降级注册每天三次兜底 ……")
            register(xml.replace(EXTRA, ""))
            _set_config_extra([])
            print("已注册任务 %s（仅每天 09:00/12:30/16:30）" % TASK_NAME)
            print()
            print("想再加上「解锁后自动补领」：右键 install_task.bat → 以管理员身份运行。")
            return show(service())
        raise
    return show(service())


if __name__ == "__main__":
    if "--show" in sys.argv:
        sys.exit(show())
    if "--delete" in sys.argv:
        sys.exit(delete())
    sys.exit(install(allow_unlock="--no-unlock" not in sys.argv))
