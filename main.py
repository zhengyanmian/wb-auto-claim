# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动领积分
------------------------
原理：
  1. 找到 WorkBuddy 主窗口（找不到就启动它）
  2. 全屏截图，在左下角区域**直接识别按钮本体的形态**（不再依赖卡片上的文字锚点）：
       - 白色实心药丸「立即领取」 -> 可领取，取其中心点击
       - 灰色实心药丸「今日已领」 -> 今天领过了，直接跳过
       - 两个都没有               -> 卡片不在（活动结束/被关掉/没加载）
     判据是「低饱和 + 高/中亮度 + 填充率≥0.8 的胶囊状色块」：文字块填充率只有 0.5~0.7，
     所以文案怎么改都不影响（2026-09-19 卡片改版把锚点小字换掉，曾导致连续 6 天领不到）
  3. 点击后复查：白药丸是否消失/变成灰药丸；没变就重试（顺带兼容窗口未聚焦吞掉第一次点击）

每次运行都往 history.jsonl 追加一条记录并刷新 status.txt，
用 status.bat 可随时查看「今天执行了没有 / 什么时候执行的 / 结果如何」。

用法：
  python main.py                 # 正常执行
  python main.py --dry-run       # 只看识别结果，不点击
  python main.py --force         # 忽略「已领过」判断，强制点一次
  python main.py --probe         # 打印识别到的按钮状态与坐标
  python main.py --selftest IMG  # 拿一张已保存的截图跑识别逻辑（离线自检）
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime

import cv2
import numpy as np
from PIL import ImageGrab

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_DIR = os.path.join(BASE, "logs")
CAP_DIR = os.path.join(BASE, "captures")
STATE_PATH = os.path.join(BASE, "state.json")
HISTORY_PATH = os.path.join(BASE, "history.jsonl")

# ---------------------------------------------------------------- DPI 感知
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# GetTickCount / GetTickCount64 在 kernel32，user32 没有导出（曾在 user32 上调 GetTickCount 直接崩）
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetTickCount.argtypes = []
try:
    kernel32.GetTickCount64.restype = ctypes.c_uint64
    kernel32.GetTickCount64.argtypes = []
    _HAS_TICK64 = True
except AttributeError:
    _HAS_TICK64 = False


# ---------------------------------------------------------------- 工具函数
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def log(msg, silent=False):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "claim-%s.log" % datetime.now().strftime("%Y-%m"))
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if not silent:
        print(line, flush=True)


def save_state(**kw):
    st = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
    st.update(kw)
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def append_history(result, detail, t0, extra=None):
    """每次运行都记一条，供 status.py 汇总"""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "result": result,
        "detail": detail,
        "duration": round(time.time() - t0, 1),
    }
    if extra:
        rec.update(extra)
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def refresh_status_file():
    try:
        import status
        status.write_status_file()
    except Exception:
        pass


def finish(code, result, detail, t0, silent=False, **state_kw):
    restore_cursor()              # 探测期间挪开的鼠标
    restore_foreground()          # 抬过前台就在这里统一还原，所有出口都覆盖
    append_history(result, detail, t0)
    save_state(last_result=result, last_detail=detail, **state_kw)
    refresh_status_file()
    return code


# ---------------------------------------------------------------- 窗口
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_window(keyword):
    """返回面积最大的、标题包含 keyword 的可见顶层窗口 (hwnd, rect)"""
    hits = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if keyword.lower() in buf.value.lower():
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w > 200 and h > 200:
                hits.append((w * h, hwnd, (r.left, r.top, w, h)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    if not hits:
        return None, None
    hits.sort(reverse=True)
    return hits[0][1], hits[0][2]


def ensure_visible(hwnd):
    """只在窗口被最小化时还原；不主动抢焦点，避免打断用户正在做的事"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(1.0)
    time.sleep(0.3)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def tick_ms():
    """系统运行毫秒数（与 LASTINPUTINFO.dwTime 同基准）"""
    if _HAS_TICK64:
        return kernel32.GetTickCount64()
    return kernel32.GetTickCount()


def idle_seconds():
    """返回用户无操作时长（秒）。任何 API 异常都退化为「已空闲」，绝不因此中断领取。"""
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 999.0
        # dwTime 是 32 位，低 32 位无符号减法可正确跨越 ~49.7 天回绕
        delta = (tick_ms() - info.dwTime) & 0xFFFFFFFF
        return max(0.0, delta / 1000.0)
    except Exception as e:
        log("读取空闲时间失败（忽略，继续执行）: %r" % (e,))
        return 999.0


def wait_user_idle(need=4.0, timeout=120.0, silent=False):
    """等用户停手几秒再动手，避免点鼠标时打断用户操作"""
    t0 = time.time()
    while True:
        if idle_seconds() >= need:
            return True
        if time.time() - t0 > timeout:
            log("用户持续在操作电脑，等待超时，仍继续执行（会自动还原鼠标位置）", silent)
            return False
        time.sleep(1.0)


def get_cursor():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


# --------------------------------------------------- 锁屏检测
def is_locked():
    """工作站是否处于「锁屏 / 未登录」。

    2026-10 实测教训：脚本记了 13 次「被 xxx 盖住 + 抬前台失败」，看失败截图才发现
    那个「盖住它的窗口」其实是**锁屏界面**（hwnd 固定 196850）——锁屏时谁也抬不起来，
    之前把根因写成「被遮挡」把人往错方向引。用 OpenInputDesktop 直接问系统，
    拿不到输入桌面就是锁屏（比猜窗口类名可靠）。"""
    try:
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        h = user32.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
        if h:
            user32.CloseDesktop(h)
            return False
        return True
    except Exception:
        pass
    try:                                                # 兜底：前台是锁屏窗口
        fg = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fg, buf, 256)
        return buf.value in ("LockScreen", "Windows.UI.Core.CoreWindow")
    except Exception:
        return False


# --------------------------------------------------- 前台窗口（卡片被盖住时才用）
_RAISED_FROM = None          # 抬 WorkBuddy 到前台之前，最前面是哪个窗口
_RAISE_LOGGED = False
_TOPMOSTED = False           # 是否用了「非激活置顶」兜底，用完要撤掉


def force_foreground(hwnd):
    """把窗口抬到最上层。三级手段，逐级降级：

    1. ALT 微按 + SetForegroundWindow —— 解除前台锁，最常见也最温和；
    2. AttachThreadInput 挂到当前前台线程上再抢 —— 手段 1 被拒时用（2026-10-11 就是被拒的）；
    3. SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE) —— 完全不抢焦点，只把窗口画到最上层。
       焦点还在用户自己那个窗口上，所以对用户几乎无感；用完在 restore_foreground() 里撤掉。
    """
    global _TOPMOSTED
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    try:
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        if user32.GetForegroundWindow() == hwnd:
            return True
        # 1)
        user32.keybd_event(0x12, 0, 0, 0)      # ALT down
        user32.keybd_event(0x12, 0, 2, 0)      # ALT up
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        if user32.GetForegroundWindow() == hwnd:
            return True
        # 2)
        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        if fg_thread:
            user32.AttachThreadInput(fg_thread, cur, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if fg_thread:
                user32.AttachThreadInput(fg_thread, cur, False)
        time.sleep(0.5)
        if user32.GetForegroundWindow() == hwnd:
            return True
        # 3)
        if user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags):   # HWND_TOPMOST
            _TOPMOSTED = True
            log("改用「非激活置顶」把窗口画到最上层（不抢你的焦点）")
            time.sleep(0.8)
            return True
    except Exception as e:
        log("抬前台异常（忽略）: %r" % (e,))
    return False


def restore_foreground():
    """把用户原本在最前面的窗口切回去 + 撤掉置顶（finish() 里统一调用）"""
    global _RAISED_FROM, _TOPMOSTED
    if _TOPMOSTED:
        try:
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            hwnd, _ = find_window(load_config()["window_title_keyword"])
            if hwnd:
                user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0,   # HWND_NOTOPMOST
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:
            pass
        _TOPMOSTED = False
    if _RAISED_FROM:
        try:
            user32.SetForegroundWindow(_RAISED_FROM)
            time.sleep(0.3)
        except Exception:
            pass
        _RAISED_FROM = None


# ---------------------------------------------------------------- 抓屏 / 匹配
def grab():
    img = ImageGrab.grab(all_screens=False)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR), img


def match(screen_bgr, tpl_path, roi=None, threshold=0.85):
    """在 roi(比例) 范围内模板匹配，返回 (score, x, y, w, h) 屏幕绝对坐标"""
    tpl = cv2.imread(tpl_path, cv2.IMREAD_COLOR)
    if tpl is None:
        raise RuntimeError("模板读取失败: " + tpl_path)
    th, tw = tpl.shape[:2]
    H, W = screen_bgr.shape[:2]
    if roi:
        x1 = int(W * roi["x1"]); y1 = int(H * roi["y1"])
        x2 = int(W * roi["x2"]); y2 = int(H * roi["y2"])
    else:
        x1, y1, x2, y2 = 0, 0, W, H
    x1 = max(0, x1); y1 = max(0, y1)
    sub = screen_bgr[y1:y2, x1:x2]
    if sub.shape[0] < th or sub.shape[1] < tw:
        return 0.0, None, None, tw, th
    res = cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return float(score), x1 + loc[0], y1 + loc[1], tw, th


# ------------------------------------------------------- 按钮识别（不依赖文字）
def _pill_boxes(hsv, x1, y1, vmin, vmax, smax, min_fill, bg_v=None, min_delta=0.0,
                rel_x_max=None, rel_y_min=None):
    """在 HSV 图里找「药丸/胶囊」状的实心色块：亮度在 [vmin,vmax]、低饱和、填充率高。
    文字块填充率通常 0.5~0.7，药丸（实心圆角矩形）≥0.9，用填充率就能把两者分开。

    bg_v / min_delta：额外要求色块比卡片底色亮 min_delta 以上。窗口失焦（被弹窗压住）时
    整个界面会整体变暗，灰药丸实测能从 V≈88 掉到 V≈50，光靠绝对阈值会漏检；
    用「比底色亮」这个相对判据就能同时兼容正常态和变暗态。

    rel_x_max / rel_y_min：在搜索框内的相对位置约束（0~1）。卡片按钮固定在左下角，
    而别的页面（比如全屏的浏览器控制台）里也有大块深灰圆角矩形，宽度和填充率都撞得上——
    2026-10 回归时 9/20~9/23 那几张「浏览器盖住屏幕」的截图就被误判成 claimed。
    真按钮实测 rel_x≈0.42 / rel_y≈0.74，假候选 rel_x≈0.80，用位置就能分开。"""
    rh, rw = hsv.shape[:2]
    mask = cv2.inRange(hsv, (0, 0, vmin), (180, smax, vmax))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        box_area = bw * bh
        if box_area < 1200:
            continue
        fill = cv2.contourArea(c) / float(box_area)
        ar = bw / float(bh)
        if fill < min_fill or not (60 <= bw <= 320 and 22 <= bh <= 80) or not (1.4 <= ar <= 6.0):
            continue
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        if rel_x_max is not None and cx / float(rw) > rel_x_max:
            continue
        if rel_y_min is not None and cy / float(rh) < rel_y_min:
            continue
        inner = float(hsv[by:by + bh, bx:bx + bw, 2].mean())
        if bg_v is not None and inner < bg_v + min_delta:
            continue
        out.append({"bbox": (bx + x1, by + y1, bw, bh),
                    "center": (bx + x1 + bw // 2, by + y1 + bh // 2),
                    "fill": round(fill, 2), "v": round(inner)})
    out.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))
    return out


def card_looks_present(bgr, cfg):
    """兜底判断「卡片到底在不在」：卡片里有两行亮色文字，实测卡片在 ≈2~7% 亮像素、
    没有卡片时该区域 ≈0%。不用模板，改版也不会失效。"""
    H, W = bgr.shape[:2]
    roi = cfg.get("card_text_roi", {"x1": 0.01, "y1": 0.70, "x2": 0.17, "y2": 0.97})
    sub = bgr[int(H * roi["y1"]):int(H * roi["y2"]), int(W * roi["x1"]):int(W * roi["x2"])]
    if sub.size == 0:
        return False, 0.0
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    m = (hsv[:, :, 2] >= 160) & (hsv[:, :, 1] <= 70)
    ratio = float(m.mean())
    return ratio >= cfg.get("card_text_min_ratio", 0.003), round(ratio, 4)


def _green_points(hsv, cfg):
    """判断卡片里有没有「绿色数字」，返回 (score, boxes)。

    2026-10-18 版本更新后，左下角改成**三张卡片轮播**（每张停 ~7.5 秒）：
        ① Buddy加油站 / 每日可领 100 积分 / 【立即领取】   <- 只有这张是领积分的
        ② 积分包购买全面开放 / 200积分起购 / 【限时加购】
        ③ 做任务赢积分好礼 / 连登抽取 Buddy 周边 / 【立即前往】
    三张的按钮**都是白色药丸**，旧逻辑「看到白药丸就点」必然点到「立即前往/限时加购」，
    于是 10-14~10-17 连续报「点了但状态没变」。三张卡唯一稳定差异是①上那个绿色「100」。

    但不能只数绿色像素：窗口底部的绿色头像、侧栏的绿色小点也在同一片区域里
    （实测 s00 帧底部 83 px、10-15 那张邀请卡 224 px，都会被误判成积分卡）。
    真数字的形态很规整：**同一水平行上并排 2~4 个、高 14~32px、宽 5~22px 的小方块**
    （"100" 实测就是 3 块 y 完全相同、宽 11~13）。用这个形态判据，头像那种长条块
    （w=30 h=9 / w=18 h=6）全部被排除。
    """
    lo = tuple(cfg.get("green_hsv_lo", [52, 80, 110]))
    hi = tuple(cfg.get("green_hsv_hi", [98, 255, 255]))
    cand = []
    # 两档阈值：正常亮度 + 窗口失焦整体变暗（饱和度下降）时放宽
    for (s_lo, v_lo) in ((lo[1], lo[2]), (max(40, lo[1] - 30), max(70, lo[2] - 40))):
        mask = cv2.inRange(hsv, (lo[0], s_lo, v_lo), hi)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            bx, by, bw, bh = cv2.boundingRect(c)
            if not (13 <= bh <= 34 and 4 <= bw <= 24):        # 单个数字的尺寸
                continue
            if not (0.25 <= bw / float(bh) <= 1.6):
                continue
            if cv2.contourArea(c) < 25:
                continue
            cand.append((bx, by, bw, bh))
        if cand:
            break
    if not cand:
        return 0, []
    # 按 y 中心聚类：同一行里至少有 2 个数字块，才算「绿色数值」
    cand.sort(key=lambda b: b[1] + b[3] / 2.0)
    best = []
    for b in cand:
        cy = b[1] + b[3] / 2.0
        row = [x for x in cand if abs((x[1] + x[3] / 2.0) - cy) <= 6]
        if len(row) > len(best):
            best = row
    if len(best) < 2:
        return 0, []                                         # 单个绿块 → 头像/装饰，不算
    score = sum(cv2.contourArea(np.array([[[b[0], b[1]]], [[b[0] + b[2], b[1]]],
                                         [[b[0] + b[2], b[1] + b[3]]], [[b[0], b[1] + b[3]]]]))
                for b in best)
    return int(score), best


def find_claim_button(bgr, cfg):
    """识别左下角「积分卡」及其按钮状态，返回 (state, cx, cy, info)。

    state:
      claimable  —— 轮到积分卡，且按钮是白色药丸「立即领取」-> 可以点
      claimed    —— 轮到积分卡，按钮已变灰「今日已领」      -> 今天领过了
      other_card —— 卡片在，但轮播到了另外两张（无绿色）     -> 等它转回来
      none       —— 卡片区域空的                          -> 卡片被关掉/没加载

    判据层次（从稳到弱）：
      1. 绿色像素占比 —— 区分「是不是积分卡」（三张卡唯一稳定差异）
      2. 白/灰药丸形态 —— 区分「领没领」（不依赖任何文案，改版免疫）
      3. 相对位置约束 —— 排除浏览器里的大灰块等误判
    """
    H, W = bgr.shape[:2]
    roi = cfg.get("pill_roi", {"x1": 0.0, "y1": 0.66, "x2": 0.34, "y2": 0.93})
    x1, y1 = int(W * roi["x1"]), int(H * roi["y1"])
    x2, y2 = int(W * roi["x2"]), int(H * roi["y2"])
    sub = bgr[y1:y2, x1:x2]
    if sub.size == 0:
        return "none", None, None, {}
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    bg_v = float(np.median(hsv[:, :, 2]))                  # 卡片底色亮度（自适应，抗主题/变暗）

    green_px, green_boxes = _green_points(hsv, cfg)
    is_points_card = green_px >= cfg.get("green_min_px", 120)

    white = _pill_boxes(hsv, x1, y1, 180, 255, 40, 0.80,                     # 白药丸 = 未领取
                        rel_x_max=0.62, rel_y_min=0.45)
    gray = []
    if not white:
        # 灰药丸 = 今日已领。先按正常亮度找，再放宽到「比底色亮就算」——
        # 2026-10-10 实测：窗口被弹窗压住时整体变暗（灰药丸 V 88 -> 50），收窄的阈值会漏检。
        for lo, hi, delta in ((45, 125, 0.0), (25, 150, 10.0)):
            gray = _pill_boxes(hsv, x1, y1, lo, hi, 60, 0.80, bg_v=bg_v, min_delta=delta,
                               rel_x_max=0.62, rel_y_min=0.45)
            if gray:
                break

    info = {"green_px": green_px, "white_n": len(white), "gray_n": len(gray),
            "bg_v": round(bg_v), "green_boxes": len(green_boxes)}

    if is_points_card:
        if white:
            c = white[0]
            return "claimable", c["center"][0], c["center"][1], info
        if gray:
            c = gray[0]
            return "claimed", c["center"][0], c["center"][1], info
        # 积分卡在但没认出按钮（加载中/动画中），当作「再等等」
        return "none", None, None, info

    if white or gray:
        return "other_card", None, None, info       # 轮播到别的卡片了
    return "none", None, None, info


# ---------------------------------------------------------------- 鼠标
def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP


def _frame_sig(bgr, cfg):
    """卡片区域缩略签名，用来判断轮播有没有在动（切换是瞬时的，0.8s 采样能抓到）。"""
    H, W = bgr.shape[:2]
    roi = cfg.get("pill_roi", {"x1": 0.0, "y1": 0.66, "x2": 0.34, "y2": 0.93})
    sub = bgr[int(H * roi["y1"]):int(H * roi["y2"]), int(W * roi["x1"]):int(W * roi["x2"])]
    if sub.size == 0:
        return None
    return cv2.resize(sub, (max(1, sub.shape[1] // 4), max(1, sub.shape[0] // 4))).astype(np.int16)


def poll_points_card(cfg, timeout, silent=False, still_limit=None, none_limit=8):
    """轮询直到命中积分卡，或判定「轮播已停」/「卡片根本不在」。

    返回 dict: state / cx / cy / bgr / info / seen_other / moved / still

    2026-10-18 实测的两个界面状态：
      未领 —— 三张卡每 ~7.5 秒切换一次，积分卡 1/3 时间在场（moved=True 会持续刷新）
      已领 —— 积分卡直接从轮播里**被移除**，轮播随之**停住**停在「积分包购买」那张，
              连鼠标移开也没有任何变化（still 会一路涨到 18 秒以上）
    所以「画面长时间完全静止 + 只剩别的卡」就是已领的可靠信号。

    none_limit：连续多少次「卡片区域全空」就提前返回。用户领完往往顺手把卡片收起，
    那时再等 45 秒也没意义，纯浪费时间（实测每次要空等近 1 分钟）。
    """
    t0 = time.time()
    interval = cfg.get("poll_interval", 0.8)
    res = {"state": "none", "cx": None, "cy": None, "bgr": None, "info": {},
           "seen_other": False, "moved": False, "still": 0.0}
    prev = None
    last_move_t = t0
    none_streak = 0
    while True:
        bgr, _ = grab()
        st, cx, cy, info = find_claim_button(bgr, cfg)
        sig = _frame_sig(bgr, cfg)
        if prev is not None and sig is not None and sig.shape == prev.shape:
            if float(np.abs(sig - prev).mean()) > 0.35:
                res["moved"] = True
                last_move_t = time.time()
        prev = sig
        if st == "other_card":
            res["seen_other"] = True
        none_streak = none_streak + 1 if st == "none" else 0
        res.update(state=st, cx=cx, cy=cy, bgr=bgr, info=info,
                   still=round(time.time() - last_move_t, 1))
        if st in ("claimable", "claimed"):
            return res
        if still_limit and res["still"] >= still_limit:
            return res
        if none_streak >= none_limit:          # 卡片压根不在，别空等
            return res
        if time.time() - t0 >= timeout:
            return res
        time.sleep(interval)


def cursor_on_card(cfg):
    """鼠标是否停在卡片上（悬停会暂停轮播，导致误判「轮播停了=已领」）"""
    x, y = get_cursor()
    H = user32.GetSystemMetrics(1)
    W = user32.GetSystemMetrics(0)
    roi = cfg.get("pill_roi", {"x1": 0.0, "y1": 0.66, "x2": 0.34, "y2": 0.93})
    return (W * roi["x1"] <= x <= W * roi["x2"]) and (H * roi["y1"] <= y <= H * roi["y2"])


_PARKED_CURSOR = None


def park_cursor(cfg, silent=False):
    """探测期间把鼠标挪出卡片区（悬停暂停轮播）；返回原坐标供还原。"""
    global _PARKED_CURSOR
    if not cursor_on_card(cfg):
        return None
    saved = get_cursor()
    W, H = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    _PARKED_CURSOR = saved
    set_cursor(int(W * 0.5), int(H * 0.25))
    log("鼠标正停在卡片上（会暂停轮播），临时移开 %s -> (%d,%d)"
        % (saved, int(W * 0.5), int(H * 0.25)), silent)
    time.sleep(0.4)
    return saved


def restore_cursor():
    global _PARKED_CURSOR
    if _PARKED_CURSOR:
        try:
            set_cursor(*_PARKED_CURSOR)
        except Exception:
            pass
        _PARKED_CURSOR = None


# ---------------------------------------------------------------- 主流程
def run(dry_run=False, force=False, probe=False, silent=False):
    t0 = time.time()
    cfg = load_config()
    keyword = cfg["window_title_keyword"]
    today = datetime.now().strftime("%Y-%m-%d")

    log("===== 开始执行 =====", silent)

    # 0. 锁屏直接退出。锁屏时截到的是锁屏界面、点击也落不到应用上，
    #    硬跑只会留下「找不到卡片 / 被 xxx 盖住」这种误导性记录（2026-10 踩过）。
    if is_locked():
        log("当前处于锁屏状态，无法操作（解锁后会自动补领当天积分）。", silent)
        return finish(6, "locked", "锁屏中，无法操作（解锁后会自动补领）", t0, silent)

    # 0b. 本地已记录今天领过 -> 直接跳过。
    #     2026-10-18 起「已领」在界面上表现为积分卡被移出轮播、轮播停住，识别成本高
    #     （要观察 20 秒静止），有了本地记录就不必每次都去验证。
    if not force:
        st_old = {}
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    st_old = json.load(f)
            except Exception:
                st_old = {}
        if st_old.get("last_claim_date") == today and st_old.get("last_result") == "success":
            log("今日已领取（本地记录 %s），跳过。" % st_old.get("last_claim_at", ""), silent)
            return finish(0, "already_claimed", "今日已领过（本地记录），跳过", t0, silent,
                          last_state="claimed")

    # 1. 找窗口
    hwnd, rect = find_window(keyword)
    launched = False
    if hwnd is None:
        app = cfg.get("app_path", "")
        if app and os.path.exists(app):
            log("未检测到 %s 窗口，正在启动: %s" % (keyword, app), silent)
            subprocess.Popen([app])
            launched = True
            deadline = time.time() + cfg.get("launch_wait", 90)
            while time.time() < deadline and hwnd is None:
                time.sleep(3)
                hwnd, rect = find_window(keyword)
        if hwnd is None:
            log("失败：找不到 %s 窗口，且无法启动" % keyword, silent)
            return finish(3, "window_not_found", "WorkBuddy 窗口未找到且无法启动", t0, silent)
    log("窗口就绪 hwnd=%s rect=%s 新启动=%s" % (hwnd, rect, launched), silent)
    ensure_visible(hwnd)

    # 2. 探测轮播：等积分卡转过来；轮播长时间停住则说明今天已领
    global _RAISED_FROM, _RAISE_LOGGED
    cycle_wait = cfg.get("cycle_wait", 45)
    still_limit = cfg.get("still_to_claimed", 18)
    park_cursor(cfg, silent)                     # 悬停会暂停轮播，先挪开
    deadline = time.time() + (max(cycle_wait, cfg.get("card_wait", 60)) if launched else cycle_wait)
    state, bx, by, screen_bgr, info, res = "none", None, None, None, {}, {}
    locked_now = False
    while True:
        res = poll_points_card(cfg, max(0.5, deadline - time.time()), silent, still_limit=still_limit)
        state, bx, by, screen_bgr, info = res["state"], res["cx"], res["cy"], res["bgr"], res["info"]
        if state in ("claimable", "claimed"):
            break
        # 只剩别的卡片 + 画面完全不动 -> 积分卡已被移出轮播（今天领过了）
        if res["seen_other"] and res["still"] >= still_limit:
            break
        if time.time() >= deadline:
            break
        # 没轮到积分卡：可能是被别的窗口盖住（实测：浏览器全屏时整屏都是浏览器）
        if is_locked():
            locked_now = True
            log("检测到锁屏（不是被别的窗口盖住），不再等待；解锁后会自动补领", silent)
            break
        if cfg.get("raise_if_covered", True) and _RAISED_FROM is None:
            fg = user32.GetForegroundWindow()
            if fg != hwnd:
                if wait_user_idle(cfg.get("idle_wait", 4), cfg.get("idle_timeout", 120), silent):
                    log("卡片不在画面里（可能被 %s 盖住），把 WorkBuddy 抬到前台重试" % fg, silent)
                    _RAISED_FROM = fg
                    if force_foreground(hwnd):
                        if not _RAISE_LOGGED:
                            _RAISE_LOGGED = True
                            log("已抬到前台，等你不动手时点击（做完会把窗口切回去）", silent)
                        time.sleep(1.0)
                        deadline = time.time() + cycle_wait
                        continue
                    log("抬前台失败（系统拒绝），继续按「卡片不在」处理", silent)

    log("轮播探测：state=%s moved=%s still=%ss %s"
        % (state, res.get("moved"), res.get("still"), info), silent)

    # 只要不是「轮到积分卡且可点/已领」，就都是「这一轮没拿到」，统一在这里收口。
    # （注意 other_card 也在其中：轮播停在购买卡上说明积分卡已被移除，绝不能掉进点击分支）
    if state not in ("claimable", "claimed"):
        path = os.path.join(CAP_DIR, "fail_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(CAP_DIR, exist_ok=True)
        if screen_bgr is not None:
            cv2.imwrite(path, screen_bgr)
        if locked_now or is_locked():
            log("失败：电脑处于锁屏状态，点不了（解锁后会自动补领）。截图: %s" % path, silent)
            return finish(6, "locked", "锁屏中，无法操作（解锁后会自动补领）", t0, silent)
        if res.get("seen_other"):
            if res.get("still", 0) >= still_limit:
                log("积分卡已从轮播移除、轮播停住（今日已领过），无需操作。", silent)
                return finish(0, "already_claimed", "积分卡已从轮播移除（今日已领过）", t0, silent,
                              last_seen_date=today, last_state="claimed")
            # 轮播在转，但整整一轮（cycle_wait）都没轮到积分卡 —— 领到之后积分卡会被移出轮播，
            # 剩下的两张继续转，所以这同样是「今天已领」。
            # 留 claimed_by 标记，方便日后出问题时一眼看出「是靠缺席推出来的，不是看到灰按钮」。
            log("轮播转满整轮都没有积分卡（已领后会被移出轮播），判定今日已领。", silent)
            return finish(0, "already_claimed", "积分卡不在轮播里（今日已领）", t0, silent,
                          last_seen_date=today, last_state="cycle_absent", claimed_by="cycle_absent")
        # 卡片不在（活动结束/被关掉/页面没加载完）。
        # 再用「该区域有没有亮色文字」区分「卡片在但按钮形态陌生」和「卡片真不在」
        title_ok, ratio = card_looks_present(screen_bgr, cfg) if screen_bgr is not None else (False, 0)
        if title_ok:
            log("警告：卡片区域有文字但没有识别到按钮（界面可能又改版了，亮像素占比 %.4f）。截图: %s"
                % (ratio, path), silent)
            return finish(4, "uncertain", "卡片在但没识别到可领按钮，界面可能改版", t0, silent,
                          last_text_ratio=ratio)
        log("卡片不在画面里（该区域亮像素占比 %.4f ≈ 无卡片）。若是你领完顺手收起了卡片，这是正常的；"
            "否则可能是被关闭 / 活动结束 / 页面未加载。截图: %s" % (ratio, path), silent)
        return finish(2, "no_card", "卡片不在（可能已收起 / 活动结束 / 未加载）", t0, silent,
                      last_text_ratio=ratio)

    if probe:
        log("probe 模式：仅定位，不点击", silent)
        return finish(0, "probe", "仅定位未点击", t0, silent, last_state=state)

    if state == "claimed" and not force:
        log("今日已领过（积分卡按钮为灰色「今日已领」），无需操作。", silent)
        return finish(0, "already_claimed", "今日已领过，跳过", t0, silent,
                      last_seen_date=datetime.now().strftime("%Y-%m-%d"), last_state=state)

    if dry_run:
        log("dry-run：按钮状态=%s，若正常执行此处会点击 (%s,%s)" % (state, bx, by), silent)
        return finish(0, "dry_run", "dry-run：未点击", t0, silent, last_state=state)

    # 3. 点击：轮播窗口只有 ~7.5 秒，点之前必须双帧确认卡片没被换走
    wait_user_idle(cfg.get("idle_wait", 4), cfg.get("idle_timeout", 120), silent)
    restore_cursor()
    saved = get_cursor()
    after_bgr = screen_bgr
    clicks = 0
    max_click = cfg.get("max_click", 3)
    while clicks < max_click:
        if state != "claimable":
            r2 = poll_points_card(cfg, cycle_wait, silent)
            state, bx, by, after_bgr, info = r2["state"], r2["cx"], r2["cy"], r2["bgr"], r2["info"]
            log("  复查轮播：state=%s %s" % (state, info), silent)
            if state != "claimable":
                break
        # 双帧确认：切换是瞬时的，两帧都稳定命中同一位置才点，避免点到隔壁卡片
        b1, _ = grab()
        s1, c1x, c1y, i1 = find_claim_button(b1, cfg)
        time.sleep(0.15)
        b2, _ = grab()
        s2, c2x, c2y, i2 = find_claim_button(b2, cfg)
        if not (s1 == "claimable" and s2 == "claimable"
                and abs(c1x - c2x) <= 10 and abs(c1y - c2y) <= 10):
            log("  点击前复查：卡片刚被轮播换走（%s -> %s），等它转回来" % (s1, s2), silent)
            state = s2 if s2 == "claimed" else "other_card"
            if state == "claimed":
                break
            continue
        bx, by = c2x, c2y
        clicks += 1
        log("第 %d 次点击「立即领取」(%d, %d)" % (clicks, bx, by), silent)
        click(bx, by)
        time.sleep(0.6)
        set_cursor(*saved)                       # 鼠标立刻还原，不干扰用户
        after_bgr, _ = grab()
        state, bx, by, info = find_claim_button(after_bgr, cfg)
        log("  点击后 0.6s 状态 = %s %s" % (state, info), silent)
        if state == "claimable":
            continue                              # 没生效（被吞点击/点偏），重试
        # 卡片已被换掉：再观察 ~9 秒（> 单张停留 7.5s）。积分卡若真领到就会被移除、不再回来；
        # 若只是被自然轮播切走，9 秒内必定会转回来 -> 说明刚才没点中，继续重试。
        r3 = poll_points_card(cfg, cfg.get("verify_wait", 9), silent)
        state = r3["state"]
        if r3["bgr"] is not None:
            after_bgr = r3["bgr"]
        log("  观察 %.0fs 后状态 = %s" % (cfg.get("verify_wait", 9), state), silent)
        if state == "claimable":
            bx, by = r3["cx"], r3["cy"]
            continue
        break

    os.makedirs(CAP_DIR, exist_ok=True)
    if state == "claimable":
        path = os.path.join(CAP_DIR, "fail_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        cv2.imwrite(path, after_bgr)
        log("警告：点了 %d 次后积分卡仍可领取，需人工确认。截图: %s" % (clicks, path), silent)
        return finish(4, "uncertain", "点击后按钮仍可领取，需人工确认", t0, silent, last_state=state)

    log("成功：今日积分已领取（点击 %d 次后积分卡已从轮播中移除）" % clicks, silent)
    return finish(0, "success", "成功领取", t0, silent,
                  last_claim_date=today,
                  last_claim_at=datetime.now().isoformat(timespec="seconds"),
                  last_clicks=clicks, last_state=state)


if __name__ == "__main__":
    import traceback
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只定位不点击")
    ap.add_argument("--force", action="store_true", help="忽略已领判断，强制点一次")
    ap.add_argument("--probe", action="store_true", help="打印定位信息")
    ap.add_argument("--silent", action="store_true", help="只写日志不打印")
    ap.add_argument("--selftest", metavar="IMG", help="对一张已保存的截图跑识别逻辑（离线自检）")
    a = ap.parse_args()

    if a.selftest:
        _cfg = load_config()
        _img = cv2.imread(a.selftest, cv2.IMREAD_COLOR)
        if _img is None:
            print("读不到图片:", a.selftest)
            sys.exit(6)
        _st, _cx, _cy, _info = find_claim_button(_img, _cfg)
        _present, _ratio = card_looks_present(_img, _cfg)
        print("selftest %s" % a.selftest)
        print("  图像尺寸 = %dx%d" % (_img.shape[1], _img.shape[0]))
        print("  识别结果 = %s  中心=(%s,%s)" % (_st, _cx, _cy))
        print("  详情 = %s" % _info)
        print("  卡片区域亮像素占比 = %.4f （>=%.4f 视为卡片存在: %s）"
              % (_ratio, _cfg.get("card_text_min_ratio", 0.003), _present))
        sys.exit(0)

    _t0 = time.time()
    try:
        sys.exit(run(dry_run=a.dry_run, force=a.force, probe=a.probe, silent=a.silent))
    except SystemExit:
        raise
    except BaseException:
        # 任何未预期异常都不再裸抛栈：写进日志/历史/状态，退出码 5
        tb = traceback.format_exc()
        print(tb, flush=True)
        log("崩溃：未预期异常，详见日志\n" + tb, a.silent)
        finish(5, "error", "脚本异常：%s" % tb.strip().splitlines()[-1][:200], _t0, a.silent)
        sys.exit(5)
