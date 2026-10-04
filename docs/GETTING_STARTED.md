# 🚀 新手上手指南

> 安装本 Ubuntu 定制版：`git clone --branch cleanup/win-adb-redundancy https://github.com/GiftedScout/GenericAgent.git`。下文官方安装器/ZIP 指向上游，不会自动安装此分支。

> 完全没接触过编程也没关系，跟着做就行。本分支仅适用于 Ubuntu / Linux。
>
> 如果你已经有 Python 环境，直接跳到[第 2 步](#2-配置-api-key)。

---

## 1. 安装 Python

### Ubuntu / Linux

先检查已有环境：`python3 --version`。需要隔离环境时使用 `uv venv`。

### 验证

终端 / 命令提示符里输入：

```bash
python3 --version
```

看到 `Python 3.x.x` 就 OK。

> ⚠️ **版本提示**：推荐 **Python 3.11 或 3.12**。不要使用 3.14（与 pywebview 等依赖不兼容）。

---

## 2. 配置 API Key

### 下载项目

最方便的方式是 **一键安装**（自带隔离 Python 环境 + Git + 桌面端）：

**Ubuntu / Linux**

```bash
curl -fsSL http://fudankw.cn:9000/files/ga_install.sh | bash
```

或者手动 clone（开发者）：

```bash
git clone https://github.com/lsdefine/GenericAgent.git
cd GenericAgent
uv venv && uv pip install -e ".[ui]"
```

也可以走最朴素的 ZIP：[GitHub 仓库页面](https://github.com/lsdefine/GenericAgent) → 点绿色 **Code** → **Download ZIP** → 解压到喜欢的位置。

> 💡 **让 Claude / Codex 等 Agent 帮你装**：把下面这条 curl 丢给它，它会按官方指南替你完成安装：
> ```bash
> curl -fsSL https://raw.githubusercontent.com/lsdefine/GenericAgent/refs/heads/main/docs/installation_zh.md
> ```
>
> 📖 平台差异、排障、升级流程见 [`docs/installation_zh.md`](installation_zh.md)。

### 创建配置文件

进入项目文件夹，把 `mykey_template.py` 复制一份，重命名为 `mykey.py`。

用任意文本编辑器打开 `mykey.py`，填入你的 API 信息。**选一种填就行**，不用的配置删掉或留着不管都行。

> 💡 也可以运行交互式向导 `python assets/configure_mykey.py`，按提示选择厂商、填入 Key 即可自动生成 `mykey.py`。

### 配置示例

**推荐首选：Claude 原生协议**：

```python
# 变量名同时含 'native' 和 'claude' → NativeClaudeSession（API 原生工具字段）
native_claude_config = {
    'name': 'claude',                        # /llms 显示名 & mixin 引用名
    'apikey': 'sk-xxx',                      # sk-ant- 走 x-api-key；其它走 Bearer
    'apibase': 'https://api.anthropic.com',  # 官方直连；反代渠道填对应地址
    'model': 'claude-opus-4-7',              # [1m] 后缀触发 1M 上下文 beta
    # 'fake_cc_system_prompt': True,         # CC switch / 反代渠道必须置 True
}
```

**也支持：OpenAI 原生协议**：

```python
# 变量名同时含 'native' 和 'oai' → NativeOAISession
native_oai_config = {
    'name': 'gpt',                           # /llms 显示名 & mixin 引用名
    'apikey': 'sk-xxx',
    'apibase': 'https://api.openai.com/v1',  # 自动补 /v1/chat/completions
    'model': 'gpt-5.5',
}
```

**进阶：Mixin 故障转移**（多 session 自动切换，最稳的玩法）：

```python
# llm_nos 按优先级排列；首项失败按指数退避切下一项
mixin_config = {
    'llm_nos': ['claude', 'gpt'],   # 与上面 native_* 的 name 字段对应
    'max_retries': 10,
    'base_delay': 0.5,
}
```

> 💡 完整字段说明（`thinking_type` / `reasoning_effort` / `context_win` / `proxy` / Zhipu / MiniMax / Kimi / OpenRouter 等渠道示例）见 `mykey_template.py` 顶部注释。

### 关键规则

**变量命名决定 Session 类型**（不是模型名决定的）：

| 变量名包含 | 触发的 Session | 工具协议 | 适用场景 |
|-----------|---------------|---------|---------|
| `native` + `claude` | NativeClaudeSession | API 原生 tool 字段 | **推荐首选** — Claude 原生协议 |
| `native` + `oai` | NativeOAISession | API 原生 tool 字段 | GPT/o 系列、OAI 兼容渠道 |
| `mixin` | MixinSession | 多 session 故障转移 | 最稳；要求被引用 session 全为 native |
| `claude`（不含 `native`） | ClaudeSession | 文本协议工具 | **deprecated**，后续版本可能移除 |
| `oai`（不含 `native`） | LLMSession | 文本协议工具 | **deprecated**，后续版本可能移除 |

**`apibase` 填写规则**（会自动拼接端点路径）：

| 你填的内容 | 系统行为 |
|-----------|---------|
| `http://host:2001` | 自动补 `/v1/chat/completions` |
| `http://host:2001/v1` | 自动补 `/chat/completions` |
| `http://host:2001/v1/chat/completions` | 直接使用，不拼接 |

---

## 3. 初次启动

终端里进入项目文件夹，运行：

```bash
cd 你的解压路径
python3 agentmain.py
```

这就是**命令行模式**，已经可以用了。你会看到一个输入提示符，直接打字发送任务即可。

试试你的第一个任务：

```
帮我在桌面创建一个 hello.txt，内容是 Hello World
```


---

## 4. 让 Agent 自己装依赖

Agent 启动后，只需要一句话，它就会自己搞定所有依赖：

```
请查看你的代码，安装所有用得上的 python 依赖
```

Agent 会自己读代码、找出需要的包、全部装好。

> ⚠️ 如果遇到网络问题导致 Agent 无法调用 API，可能需要先手动装一个包：
> ```bash
> pip install requests
> ```

### 升级到图形界面

依赖装完后，可以选择适合你的前端：

| 前端 | 启动命令 | 说明 |
|------|---------|------|
| **桌面端** | `python3 launch.pyw` | 真原生窗口，零终端依赖 |
| **TUI v3** | `python frontends/tui_v3.py` | 基于块的滚屏回看、resize 重排、每终端独立配色，跨终端体验一致 |
| **TUI v2** | `python frontends/tuiapp_v2.py` | Textual 键盘驱动界面，图片粘贴折叠、`/llm`/`/export`/`/continue` 选择器 |
| **Streamlit / 悬浮窗** | `python launch.pyw` | 浏览器中打开的 Streamlit UI，附带桌面悬浮窗 |


### 可选：让 Agent 帮你做的事

```
请帮我建立 git 连接，方便以后更新代码
```

Agent 会自动配好。如果你电脑上没有 Git，它也会帮你下载 portable 版。

```
请帮我在桌面创建一个 launch.pyw 的快捷方式
```

这样以后双击桌面图标就能启动，不用再开终端了。

---

## 5. 能力解锁

环境跑起来之后，你可以逐步解锁更多能力。每一项都只需要**对 Agent 说一句话**：

### 基础能力

| 能力 | 对 Agent 说 | 说明 |
|------|-----------|------|
| **全局文件搜索** | `安装并配置 plocate / ripgrep` | 索引文件搜索 / 项目文本搜索 |


### 进阶能力

| 能力 | 对 Agent 说 | 说明 |
|------|-----------|------|
| **OCR** | `用rapidocr配置你的ocr能力并存入记忆` | 读取本地图片文件文字（屏幕控制暂停） |
| **Computer-use** | 未完成，暂停验收 | 禁止主动调用其原子工具 |

### 聊天平台接入（可选）

接入后可以随时随地通过手机给电脑上的 Agent 发指令。

对 Agent 说：`看你的代码，帮我配置 XX 平台的机器人接入`

支持的平台：**微信个人Bot** / QQ / 飞书 / 企业微信 / 钉钉 / Telegram

> Agent 会自动读取代码、引导你完成配置。

### 高级模式

以下模式全部**自文档化**——不用查手册，直接问 Agent 即可：

| 模式 | 对 Agent 说 |
|------|------------|
| **Reflect（反射）** | `查看你的代码，告诉我你的 reflect 模式怎么启用` |
| **计划任务** | `查看你的代码，告诉我你的计划任务模式怎么启用` |
| **Plan（规划）** | `查看你的代码，告诉我你的 plan 模式怎么启用` |
| **SubAgent（子代理）** | `查看你的代码，告诉我你的 subagent 模式怎么启用` |
| **自主探索** | `查看你的代码，告诉我你的自主探索模式怎么启用` |
| **Goal** | `查看你的代码，告诉我 goal 模式怎么启用` |
| **Goal Hive（多 worker 协作）** | `查看你的代码，告诉我 goal hive 模式怎么启用` |
| **Conductor（多 subagent 编排）** | `查看你的代码，告诉我 conductor 模式怎么启用` |
| **Morphling（吞噬外部项目）** | `查看你的代码，告诉我 morphling 模式怎么启用` |

> 💡 这就是 GenericAgent 的核心设计理念：**代码即文档**。Agent 能读懂自己的源码，所以任何功能你都可以直接问它。

---

## 💡 使用越久越强

GenericAgent 不预设技能，而是**靠使用进化**。每完成一个新任务，它会自动将执行路径固化为 Skill，下次遇到类似任务直接调用。

你不需要管理这些 Skill，Agent 会自动处理。使用时间越长，积累的技能越多，最终形成一棵完全属于你的专属技能树。

> 💡 如果你觉得某些重要信息 Agent 没有记住，可以直接告诉它：`把这个记到你的记忆里`，它会主动记忆。

**其他 Claw 的 Skill 也可以直接复用：**

- 让 Agent 搜索：`帮我找个做 XXX 的 skill` → 完成后 → `加入你的记忆中`
- 直接指定来源：`访问 XXX 文件夹/URL，按照这个 skill 做 XXX`

**保持更新：**

对 Agent 说：`git 更新你的代码，然后看看 commit 有什么新功能`

> Agent 会自动 pull 最新代码并解读 commit log，告诉你新增了什么能力。

> 更多细节请参阅 [README.md](../README.md) 或 [详细版图文教程](https://my.feishu.cn/wiki/CGrDw0T76iNFuskmwxdcWrpinPb)。
