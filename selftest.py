# -*- coding: utf-8 -*-
"""离线自检：不碰真实界面、不写真实记录，验证识别与状态机是否正常

用法：
    python selftest.py

第一部分：用 fixtures/ 里的样本图验证识别（新版的轮播三卡 + 旧版的灰药丸）
第二部分：用这些图当「假的屏幕」，喂给 run() 跑状态机，验证各种分支

什么时候跑它：
    - 每次改完 main.py
    - WorkBuddy 界面又改版、担心识别失效时（配合 make_templates.py 存新样本）

2026-10-18 界面改成三张卡片轮播后，状态机多了两个概念：
    other_card —— 卡片在，但轮到了「积分包购买 / 做任务」那两张
    no_card    —— 左下角整块是空的（用户领完顺手收起了卡片 / 活动结束）
所以仿真帧序列里要显式夹带 other_card 帧，验证「等轮播转回来」这条路径。
"""
import os
import sys
import tempfile

import cv2

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

import main as wb  # noqa: E402

FIX = os.path.join(BASE, "fixtures")
UNCLAIMED_PATH = os.path.join(FIX, "unclaimed.png")
CLAIMED_PATH = os.path.join(FIX, "claimed.png")

failed = 0

cfg = wb.load_config()

print("========== 1. 按钮识别 ==========")
fixtures = {}
for name, path, expect in (("unclaimed(旧版)", UNCLAIMED_PATH, "claimable"),
                           ("claimed(旧版灰药丸)", CLAIMED_PATH, "claimed")):
    img = cv2.imread(path)
    if img is None:
        print("[SKIP] 缺少样本 %s（跑 make_templates.py 生成）" % path)
        continue
    fixtures[expect] = img
    st, cx, cy, info = wb.find_claim_button(img, cfg)
    ok = st == expect
    failed += 0 if ok else 1
    print("%s %-20s 识别=%-9s 期望=%-9s 中心=(%s,%s) %s"
          % ("[PASS]" if ok else "[FAIL]", name, st, expect, cx, cy, info))

# 2026-10-18 新版轮播的真实全屏截图：只有「Buddy加油站」那张带绿色数字
ROUND_PATH = os.path.join(BASE, "captures", "samples", "s10.png")
OTHER_PATH = os.path.join(BASE, "captures", "samples", "s00.png")
OTHER2_PATH = os.path.join(BASE, "captures", "samples", "other_card_buy.png")
for name, path, expect in (("轮播-积分卡", ROUND_PATH, "claimable"),
                           ("轮播-任务卡", OTHER_PATH, "other_card")):
    img = cv2.imread(path)
    if img is None:
        print("[SKIP] 缺少轮播样本 %s" % path)
        continue
    st, cx, cy, info = wb.find_claim_button(img, cfg)
    ok = st == expect
    failed += 0 if ok else 1
    print("%s %-20s 识别=%-9s 期望=%-9s %s"
          % ("[PASS]" if ok else "[FAIL]", name, st, expect, info))
    if expect == "claimable":
        fixtures["claimable"] = img

print()
print("========== 2. run() 状态机仿真 ==========")
if not fixtures:
    print("[SKIP] 没有样本图，跳过")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="wb-selftest-")
wb.HISTORY_PATH = os.path.join(TMP, "history.jsonl")
wb.STATE_PATH = os.path.join(TMP, "state.json")
wb.LOG_DIR = os.path.join(TMP, "logs")
wb.CAP_DIR = os.path.join(TMP, "captures")
wb.refresh_status_file = lambda: None            # 不写真实 status.txt
wb.find_window = lambda kw: (12345, (0, 0, 2160, 1440))
wb.ensure_visible = lambda hwnd: None
wb.wait_user_idle = lambda *a, **k: True
wb.get_cursor = lambda: (10, 10)
wb.set_cursor = lambda x, y: None
wb.park_cursor = lambda cfg_, silent=False: None  # 不动真实鼠标
wb.restore_cursor = lambda: None
wb.is_locked = lambda: False

_real_sleep = wb.time.sleep
wb.time.sleep = lambda *a, **k: None             # 仿真不真等（轮播节奏靠帧序列模拟）

clicks = []


def run_case(name, frames, expect_code, expect_clicks, quiet=True):
    it = iter(frames)
    last = {"img": frames[-1]}
    if os.path.exists(wb.STATE_PATH):
        os.remove(wb.STATE_PATH)                  # 每个用例独立，避免「今天已领」快速路径串场

    def fake_grab():
        try:
            last["img"] = next(it)
        except StopIteration:
            pass
        return last["img"], None

    wb.grab = fake_grab
    wb.click = lambda x, y: clicks.append((int(x), int(y)))
    del clicks[:]
    if quiet:
        sys.stdout = open(os.devnull, "w")
    try:
        code = wb.run(silent=True)
    finally:
        if quiet:
            sys.stdout = sys.__stdout__
    ok = code == expect_code and len(clicks) == expect_clicks
    global failed
    failed += 0 if ok else 1
    print("%s %-34s 退出码=%s(期望%s) 点击数=%d(期望%d)"
          % ("[PASS]" if ok else "[FAIL]", name, code, expect_code, len(clicks), expect_clicks))


U, C = fixtures.get("claimable"), fixtures.get("claimed")
O = cv2.imread(OTHER_PATH) if os.path.exists(OTHER_PATH) else None
if U is not None and C is not None:
    run_case("已经领过(灰药丸) -> 跳过不点", [C], 0, 0)
    run_case("可领 -> 点一次成功", [U, U, U, C], 0, 1)
    run_case("轮播先给别的卡 -> 等到积分卡再点", [O, O, U, U, U, C], 0, 1)
    run_case("第一次点击被吞 -> 自动重试", [U, U, U, U, U, U, C], 0, 2)
    run_case("一直点不掉 -> uncertain", [U] * 60, 4, 3)

    blank = U.copy()
    blank[900:1400, 0:800] = 25                   # 把整个卡片区涂黑 = 用户收起了卡片
    run_case("卡片被收起 -> no_card", [blank] * 12, 2, 0)

    # 锁屏：锁屏时截图是锁屏界面、点了也白点，应该 0 次点击直接退出（退出码 6）——
    # 2026-10-11 就是锁屏被误当成「被窗口盖住」，报了一堆莫名的失败
    wb.is_locked = lambda: True
    run_case("锁屏 -> locked 不点击", [blank], 6, 0)
    wb.is_locked = lambda: False

wb.time.sleep = _real_sleep

print()
print("失败 %d 项" % failed)
sys.exit(1 if failed else 0)
