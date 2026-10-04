# Vision API SOP

## ⚠️ 前置规则（必须遵守）

1. **先核验图像来源**：普通图片直接用文件；桌面截图能力 `computer_use` 当前**未完成、暂停验收，禁止主动调用**。Ubuntu 窗口枚举可用 AT-SPI（GNOME 原生窗口）或 xdotool（仅 X11/Xwayland）；枚举不代表截图或控制能力可用。
2. **🚫 禁止全屏截图**：只分析用户提供或明确授权的局部/窗口图片；能局部不整窗，禁止绕过 computer-use 暂停状态自行截图。
3. **能不用 vision 就不用**：如果窗口标题/本地 OCR（`ocr_utils.py`）能获取所需信息，就不要调用 vision API，省 token 且更可靠。Vision 是最后手段。

## 快速用法

```python
from vision_api import ask_vision
result = ask_vision(image, prompt="描述图片内容", timeout=60, max_pixels=1_440_000)
# image: 文件路径(str/Path) 或 PIL Image
# backend: 'openai'(默认) | 'claude' | 'modelscope' | 'auto'
# 默认配置: native_oai_config0 (xAI，使用配置内代理)；文件路径/PIL Image 均已实测
# 旧配置 native_oai_config15 返回 HTTP 503；备用 native_oai_config_fr113 曾实测识图成功
# 返回 str：成功为模型回复，失败为 'Error: ...'
```

## 如果没有 `vision_api.py`，初次构建vision能力

1. 复制 `memory/vision_api.template.py` → `memory/vision_api.py`
2. 只改头部"用户配置区"：去 `mykey.py` 里扫描变量名（⚠️ 只看名字，禁止输出 apikey 值），尝试找能用配置名填入 `CLAUDE_CONFIG_KEY` / `OPENAI_CONFIG_KEY`，`DEFAULT_BACKEND` 选后端，并测试
3. 保底：没有可用 config 时去 `https://modelscope.cn/my/myaccesstoken` 申请 token 填入 `MODELSCOPE_API_KEY`
