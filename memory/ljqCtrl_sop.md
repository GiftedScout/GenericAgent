# ljqCtrl（未完成，暂停验收）

**禁止 LLM 主动调用键鼠、截图、窗口流等 computer-use 原子工具。** 已有 Ubuntu 代码保留；仅用户明确要求恢复开发/验收才可使用。参见 `computer_use.md`。下文为历史开发记录，不是已验收能力。

> Wayland 的 `GrabWindow(title)` 已接窗口流，但未完成原生激活、授权源绑定、物理坐标校准及端到端验收；旧的“明确拒绝”描述已过时。禁止使用全屏 Screenshot Portal 绕过暂停状态。

> **must call update working ckp**：`一律使用物理坐标｜禁pyautogui｜操作前先激活窗口`

## 保留开发边界
- Ubuntu/X11/Xwayland 与 Wayland 实现保留；此能力未完成，不能据历史原子测试宣称可用。
- 普通图片文件 OCR 可用；屏幕 OCR 随 computer-use 暂停。
- 恢复开发时需先解决窗口激活、授权绑定、物理坐标校准和可观察结果闭环。

## 0. API 快速参考 (Signatures)
- `ljqCtrl.dpi_scale`: float (缩放系数 = 逻辑宽度 / 物理宽度)
- `ljqCtrl.Click(x, y=None)`: 模拟点击。支持 `Click((x, y))` 或 `Click(x, y)`
- `ljqCtrl.Press(cmd, staytime=0)`: 模拟按键。如 `Press('ctrl+c')`
- `ljqCtrl.FindBlock(fn, wrect=None, threshold=0.8)`: 找图。返回 `((center_x, center_y), is_found)`
- `ljqCtrl.GrabWindow(hwnd_or_name)`: 前台截图(先Activate), 传hwnd(int)或窗口标题子串(str), 返回PIL Image
- `ljqCtrl.GrabWindowBg(hwnd_or_name, timeout=5)`: 保留接口，Ubuntu 实现未完成端到端验收
- `ljqCtrl.MouseDClick(staytime=0.05)`: 鼠标双击
- 可先阅读computer_use.md

## 1. 环境载入
import ljqCtrl

## Ubuntu 恢复开发检查项（未验收）

- 输入坐标与截图像素坐标的关系需按实际输出缩放校准，不能套用旧平台公式。
- X11/Xwayland 与原生 Wayland 必须分别确认窗口身份、激活状态和截图授权来源。
- Wayland 当前仍缺原生激活、授权源绑定与端到端闭环；禁止据原子测试宣称可用。
- 点击后无可观察变化应停止定位，不盲目重试；不得用全屏截图绕过暂停状态。
- 普通图片 OCR 不受此暂停影响。恢复 computer-use 仅接受用户明确授权。
