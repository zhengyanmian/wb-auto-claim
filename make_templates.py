# -*- coding: utf-8 -*-
"""诊断/取图工具：截当前屏幕，打印识别结果，并保存卡片区域图供人工确认

现在主流程**不依赖任何模板**（直接识别按钮形态），所以「领不到」时不需要重跑本脚本。
它的用途是：
    - 想确认「现在识别成什么状态」 -> python make_templates.py
    - 界面又改版、要排查为什么识别不到 -> 它会存一张卡片区域放大图到 captures/
    - 把某张截图存成测试样本 -> python make_templates.py 图片路径
"""
import os
import sys

import cv2
import numpy as np
from PIL import ImageGrab

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as wb  # noqa: E402  复用同一套识别逻辑，避免两处实现不一致

BASE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(BASE, "captures")
FIX = os.path.join(BASE, "fixtures")
os.makedirs(CAP, exist_ok=True)
os.makedirs(FIX, exist_ok=True)

cfg = wb.load_config()

if len(sys.argv) > 1:
    bgr = cv2.imread(sys.argv[1], cv2.IMREAD_COLOR)
    if bgr is None:
        print("读不到图片:", sys.argv[1])
        sys.exit(1)
    src = sys.argv[1]
else:
    bgr = cv2.cvtColor(np.array(ImageGrab.grab(all_screens=False)), cv2.COLOR_RGB2BGR)
    src = "（现场截屏）"
    cv2.imwrite(os.path.join(FIX, "current_screen.png"), bgr)

H, W = bgr.shape[:2]
print("来源: %s  尺寸 %dx%d" % (src, W, H))

state, cx, cy, info = wb.find_claim_button(bgr, cfg)
present, ratio = wb.card_looks_present(bgr, cfg)
print("识别结果: state=%s  中心=(%s,%s)" % (state, cx, cy))
print("          卡片区域亮像素占比=%.4f（卡片存在=%s）" % (ratio, present))
print("          详情=%s" % info)
print("  文案对照：claimable=白药丸「立即领取」可点 / claimed=灰药丸「今日已领」跳过 / none=卡片不在")

roi = cfg["pill_roi"]
y1, y2 = int(H * 0.72), int(H * 0.98)
x1, x2 = int(W * roi["x1"]), int(W * roi["x2"])
crop = bgr[y1:y2, x1:x2]
cv2.imwrite(os.path.join(CAP, "card_region.png"),
            cv2.resize(crop, (crop.shape[1] * 2, crop.shape[0] * 2), interpolation=cv2.INTER_LANCZOS4))
print("卡片区域放大图: captures/card_region.png（x %d~%d, y %d~%d）" % (x1, x2, y1, y2))

if state == "claimable":
    bx, by, bw, bh = info["pill"]["bbox"]
    cv2.imwrite(os.path.join(FIX, "unclaimed.png"), bgr)
    print("当前是可领状态，已把整屏存为 fixtures/unclaimed.png（可给 selftest.py 用）")
elif state == "claimed":
    cv2.imwrite(os.path.join(FIX, "claimed.png"), bgr)
    print("当前是已领状态，已把整屏存为 fixtures/claimed.png（可给 selftest.py 用）")
else:
    print("当前画面没识别到按钮：卡片不在 / 被关掉 / 界面又改版了。")
    print("先看 captures/card_region.png 确认界面长什么样，再决定改识别参数还是等卡片出现。")
