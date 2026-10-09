# SSH 工具：使用与验收

## 范围

轻量封装原生 OpenSSH，不安装远端常驻服务，不设置命令白名单，不替代现有授权规则。

仅注册一个工具 **`ssh_run`**，额外封装都是它的可选项，不另设工具入口。

- `ssh_run`（省略 `action` 或 `action="run"`）：远程 Bash/Python，支持 `cwd`、`env`、指定解释器、原生 SSH 参数；前台返回退出码、输出预览及本地完整输出文件。
- `ssh_run(background=true)`：使用 `nohup + setsid` 提交，立即返回 `task_id`；后台运行时间不受提交 timeout 限制。
- `ssh_run(action="status"/"logs"/"wait"/"stop", task_id=...)`：查询/管理后台任务；日志支持 byte offset 和 `next_offset`；stop 验证 PID 启动时间与进程组后发送 TERM。
- `ssh_run(action="upload"/"download", local_path=..., remote_path=...)`：通过现代 OpenSSH scp/SFTP 上传下载，复用相同连接。

连接归属单个 agent，以 host 与 SSH 参数为键复用 ControlMaster。默认空闲 1800 秒自动关闭；agent 正常结束、异常或用户中断时主动关闭，远端后台任务不随连接关闭。复用的是传输连接，各调用仍是独立 shell。

## 示例

```json
{"host":"my-server","type":"bash","script":"python3 train.py > train.log 2>&1","cwd":"~/project","background":true,"task_id":"train-20261009"}
```

以下均调用同一个 `ssh_run`，随后使用返回的 ID（查询无需 script）：

```json
{"host":"my-server","task_id":"train-20261009","action":"status"}
```

```json
{"host":"my-server","task_id":"train-20261009","action":"logs","offset":0}
```

增量日志下一次使用 `next_offset`。若指定了 `task_root`，后续查询必须使用相同路径。

上传示例（下载将 `action` 改为 `download`）：

```json
{"host":"my-server","action":"upload","local_path":"data.bin","remote_path":"/tmp/data.bin"}
```

`action` 默认为 `run`，只需提供对应操作的参数；上传下载的本地相对路径以 agent 工作目录解析。

## 边界与依赖

- 本地需要 `ssh` / `scp`；身份验证使用 SSH config、agent 或密钥文件路径，工具不读取密钥内容。
- 后台任务管理针对 Linux，依赖 `/proc`、`nohup`、`setsid`、`base64` 等常见工具；并非调度器，不提供重启恢复。
- `timeout=0` 表示不限等待；长任务推荐后台提交，而不是无限前台等待。
- 前台 timeout/中断只保证本地 SSH 等待结束，不保证远端进程已停止；返回 `remote_state=unknown`。需要可靠跟踪和停止的工作应后台提交。
- 提交遇网络错误时先查询相同 `task_id`，不要生成新 ID 盲重试。已有 ID 不会重复执行；任务目录不自动删除。
- `stop` 是 TERM 请求，不承诺强杀忽略 TERM 的任务，也不覆盖任务自行创建的独立 session。
- 输出预览有限长，完整前台输出保存在 `stdout_path` / `stderr_path`；字节日志片段解码采用 replacement，跨片段 Unicode 不保证完整显示。
- scp 失败可能留下不完整文件；显式端口建议使用 `ssh_options=["-o","Port=2222"]` 而非 SSH 的 `-p`。
- 本功能已合入 main；已运行的 agent 不会自动刷新导入代码和工具 Schema，需新启动 agent 使用。合并标记与回退检查见 [合并与回退说明](ssh_tools_merge.md)。

## 可重跑测试

提供可访问的 Linux SSH 测试账户（测试会写 `~/.cache/ga-ssh/test-*` 和 `/tmp`，并执行短任务）：

```bash
export GA_SSH_TEST_HOST='user@host'
export GA_SSH_TEST_OPTIONS='["-o","BatchMode=yes"]'
python3 -m unittest discover -s tests -p 'test_ssh_tools.py' -v
```

未提供 host 时，真实 SSH 测试会跳过，不能据此宣称集成验收通过。

## 本次验收证据

使用隔离 Docker SSH 服务 `root@127.0.0.1:32768`，非 mock 远端；测试认证文件仅按路径引用，不提交仓库。

九项测试通过，涵盖：

1. agent 主循环正常、异常、abort 收尾释放真实连接，后台任务仍完成。
2. 后台断连续跑、稳定 ID 幂等提交、wait 与缺失任务。
3. handler 真实 dispatch、ControlMaster socket 复用、独立 shell 状态。
4. 非零退出、stderr、前台 timeout 和停止信号。
5. 二进制/Unicode 日志字节 offset、伪造元数据隔离、大输出截断及完整文件。
6. 后台进程组停止与退出码 143。
7. 含空格路径上传下载和空闲连接关闭。
8. 引号与任务 ID 校验。
9. Schema 与错误 dispatch。

额外独立实测通过：无限等待与环境变量字面量、缺失 cwd、后台解释器缺失返回 127、缺失日志返回 error、close 后重新连接。

### 全库回归修复

原基线的 `1 failure / 13 errors` 已在本分支处理，不删除有效测试、不新增 skip：

- 旧脚本顶层 `sys.exit()` 导致 discovery 导入失败，并有模块级 HTTP mock 污染后续测试：脚本主体改为仅直接执行时运行，discovery 通过子进程 TestCase 执行原断言。
- Responses 测试的模拟响应补齐上下文管理协议，与实际 HTTP 客户端一致。
- spinner 测试用 `builtins` 模块识别内建名，修复导入时将合法 `int` 误报为未定义的问题。
- Schema 文件使用上下文管理关闭，修复观察到的 ResourceWarning。

最终启用真实 SSH fixture 运行全库：**115 项，113 通过、2 个原有可选测试跳过，0 failure、0 error**。两项跳过分别是按既有要求暂停的 computer-use 检查，以及需要 `GA_RESTORE_TEST_LOG` 指定真实历史日志的回归；SSH 九项均运行通过。

单入口另外通过五项独立真实 dispatch 探测：`timeout=0` 与环境变量字面量、缺失任务返回 `task_state=missing`、非法 action、缺失 cwd 不执行脚本、错误后连接仍可执行。

本地证据：`temp/ssh_test_env/single_tool_final_regression.log`、`single_tool_independent.json`、`single_tool_acceptance.log`、`baseline.log`（相对于主仓库；不包含密钥内容，不随功能提交）。

单入口 SSH 功能与本次全库回归：`VERDICT: PASS`（上述两项可选检查未验收）。

### 当前模型真实新会话验收

代码链路验收之后另完成当前模型新会话实测：17 次 `ssh_run`，约 12 秒后台提交 elapsed=0.051 秒，独立核对原始返回、远端状态与下载文件哈希；包含首测失败、schema 问题与修复边界，见 [模型验收报告](ssh_model_acceptance.md)。随用随发的非基本工具入口与约束见 `memory/subagent.md`。
