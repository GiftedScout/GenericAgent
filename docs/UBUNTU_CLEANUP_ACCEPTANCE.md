# Ubuntu 个人定制验收（2026-10-04）

## 范围与结论

分支：`cleanup/win-adb-redundancy`。在独立 worktree 修改，未切换、合并或修改 `main`。
本轮为此前清理补齐当前上游前后端的 Linux 化，并保持已合入的模型配置格式。

**已验范围：通过。发布包完整验收：未完成。computer-use：未完成、暂停验收。**
不以编译、原子控制或窗口出现冒充完整桌面对话/Wayland 控制验收。

## 主要修改

- 后端执行器和工具 schema 统一 Python/Bash；移除 PowerShell、Win32 子进程/编码分支。
- Shell 使用可执行 `$SHELL`，缺失时回退 Bash/sh；workspace 用 Linux symlink，拒绝清理真实目标目录。
- ACP 保留 POSIX 文件描述符隔离，避免日志或子进程污染 JSON-RPC stdout。
- 前端启动、桌面配置、浏览器打开、终端按键及进程生命周期移除 Windows/macOS 专用逻辑；网页版桥接改为同源，Tauri 桥接保持本机地址。
- Rust 桌面壳移除 Windows 托盘、Win/mac 专属命令/权限/路径；打包目标和发布 workflow 改为 Linux；保留第三方许可、资源哈希、来源清单、CSP/权限和发布证据校验。
- 删除 Win/mac 安装、卸载、DMG 与发布验收文件，更新 Ubuntu 安装/使用文档；注明官方安装器不等于本定制分支。
- 修复回归发现的上下文字符额度字符串转换和账本 compaction 并发丢记录问题。
- 保留 Ubuntu/Wayland 控制实现；中英文初始记忆和 computer-use SOP 明确“未完成，禁止主动调用原子工具”，普通图片 OCR 不暂停。

## 动态验证

证据位于工作树 `temp/linux_acceptance/`（临时目录不提交）。

| 检查 | 实际命令/动作 | 观察 | 结果 |
| --- | --- | --- | --- |
| 前端后端回归 | `python3 -m pytest frontends/tests -q` | 264 passed | PASS |
| Linux 独立验收与 TUI | `python3 -m pytest tests/test_linux_acceptance.py tests/test_tui_fold_window.py -q` | 6 passed | PASS |
| tests/ 隔离执行 | 每个可收集测试文件分别 pytest；脚本式测试独立执行 | 29 个文件 exit 0；暂停的控制测试未执行 | PASS |
| Rust 单元回归 | 容器中 `cargo test --locked --lib --no-default-features` | 22 passed | PASS |
| 原生构建 | 容器中 `cargo build --locked --no-default-features` | 构建成功 | PASS |
| 原生窗口烟测 | 隔离 Xvfb、临时 HOME，启动 `ga-desktop --no-autostart`，用 xwininfo 枚举 | 1280×800 主窗口与 600×580 配置/恢复窗口，观察后终止 | PASS（仅启动边界） |
| 网页链路 | 实际 Chrome 打开隔离桥接服务，读取页面、HTTP/WS、保存截图 | 页面有实际内容；pageerror/requestfailed 均为空；资源/接口无错误状态 | PASS |
| API 负例 | 不存在会话、恶意 Origin；WS ping | 404、403；bridge-ready/services.snapshot/pong | PASS |
| 执行器负例 | 真实 Bash/Python、Unicode；exit 17；不支持的 PowerShell | 文件产出/读取正确；保留退出码；拒绝旧类型 | PASS |
| workspace 负例 | Unicode/空格路径 symlink，重复建链、删除真实目录尝试 | 拒绝重复/真实目录删除；目标 sentinel 保留 | PASS |
| 提示词与 ACP | 独立临时 memory 中 zh/en prompt/schema；真实 ACP initialize 子进程 | Python/Bash schema；JSON-RPC 响应可解析 | PASS |
| 预编译分发门禁 | `node frontends/desktop/scripts/verify-compiled-dist.mjs` | distribution contracts passed | PASS |
| 静态补充 | 改动 Python AST/JSON、Linux shell `bash -n`、`git diff --check` | 无错误 | PASS |

## 明确边界与残差

1. **computer-use 未完成**：没有恢复键鼠、窗口抓帧、屏幕 OCR 的主动使用/验收；原生激活、授权源绑定、物理坐标校准、端到端操作仍欠缺。
2. **未验完整发布包**：AppImage 构建产物、离线安装/迁移/卸载全链与真实 Wayland 桌面交互没有完成此次实测；CI workflow 修改不能当作发布成功。
3. **未调用真实付费模型**：未用用户真实密钥进行完整模型对话；系统 Python 3.14 后端测试通过不代表 pywebview 全部兼容。
4. React `dist/` 是上游提供的编译产物，源码未在此仓库分发；其中仍有跨平台检测/兼容字符串。本轮不直接改压缩产物或伪造来源/哈希。因此不能宣称整个仓库“零 Windows/macOS 字符串”。本 fork 可编辑运行源码、schema、提示词与发布路径已按 Linux 裁剪。
5. 测试须分进程：`frontends/tests` 的旧夹具替换模块会污染同进程的真实后端测试。分进程复测通过；未为消除夹具错误回滚后端。
6. GitHub 代理连接超时，绕过代理直连已验证可用；不修改系统代理配置。

VERDICT: PARTIAL

原因：已验非 computer-use 的运行路径通过；完整发布包和真实桌面端到端链路仍未验证。computer-use 按用户要求排除，状态保持未完成。

## main 合并复核（2026-10-04）

经用户授权，将清理分支与当前 main 合并；前文“未修改 main”是清理提交时的历史状态，不再代表合并后的状态。
- 合并前 main：`3ca0664`；回退标签：`pre-ubuntu-main-merge-20261004`，本地备份分支：`backup/main-before-ubuntu-20261004`。
- 隔离合并无冲突；保留 main 的遗失工具结果修复与软上下文限制，不回滚近期修复。
- 合并树复测：前端 **264 passed**；Linux/遗失结果/软限制/TUI **14 passed**；tests/ 独立执行 **30 个文件全部 exit 0**；预编译分发契约通过。
- Rust 和编译前端资产与已验清理提交相同；未重新宣称 AppImage 或真实 Wayland 操控通过。
- computer-use 仍为未完成、暂停验收，禁止主动调用原子工具。
- 回退已发布的本次合并应使用 `git revert -m 1 <本次合并提交>`，不默认 hard reset 或强推；标签也可用于独立工作树检出旧版。
