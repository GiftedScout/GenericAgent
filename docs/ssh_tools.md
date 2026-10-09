# SSH 工具：使用与验收

## 范围

轻量封装原生 OpenSSH，不安装远端常驻服务，不设置命令白名单，不替代现有授权规则。

- `ssh_run`：远程 Bash/Python，支持 `cwd`、`env`、指定解释器、原生 SSH 参数；前台返回退出码、输出预览及本地完整输出文件。
- `ssh_run(background=true)`：使用 `nohup + setsid` 提交，立即返回 `task_id`；后台运行时间不受提交 timeout 限制。
- `ssh_task`：`status` / `logs` / `wait` / `stop`；日志支持 byte offset 和 `next_offset`；stop 验证 PID 启动时间与进程组后发送 TERM。
- `ssh_transfer`：通过现代 OpenSSH scp/SFTP 上传下载，复用相同连接。

连接归属单个 agent，以 host 与 SSH 参数为键复用 ControlMaster。默认空闲 1800 秒自动关闭；agent 正常结束、异常或用户中断时主动关闭，远端后台任务不随连接关闭。复用的是传输连接，各调用仍是独立 shell。

## 示例

```json
{"host":"my-server","type":"bash","script":"python3 train.py > train.log 2>&1","cwd":"~/project","background":true,"task_id":"train-20261009"}
```

随后使用返回的 ID：

```json
{"host":"my-server","task_id":"train-20261009","action":"status"}
```

```json
{"host":"my-server","task_id":"train-20261009","action":"logs","offset":0}
```

增量日志下一次使用 `next_offset`。若指定了 `task_root`，后续查询必须使用相同路径。

## 边界与依赖

- 本地需要 `ssh` / `scp`；身份验证使用 SSH config、agent 或密钥文件路径，工具不读取密钥内容。
- 后台任务管理针对 Linux，依赖 `/proc`、`nohup`、`setsid`、`base64` 等常见工具；并非调度器，不提供重启恢复。
- `timeout=0` 表示不限等待；长任务推荐后台提交，而不是无限前台等待。
- 前台 timeout/中断只保证本地 SSH 等待结束，不保证远端进程已停止；返回 `remote_state=unknown`。需要可靠跟踪和停止的工作应后台提交。
- 提交遇网络错误时先查询相同 `task_id`，不要生成新 ID 盲重试。已有 ID 不会重复执行；任务目录不自动删除。
- `stop` 是 TERM 请求，不承诺强杀忽略 TERM 的任务，也不覆盖任务自行创建的独立 session。
- 输出预览有限长，完整前台输出保存在 `stdout_path` / `stderr_path`；字节日志片段解码采用 replacement，跨片段 Unicode 不保证完整显示。
- scp 失败可能留下不完整文件；显式端口建议使用 `ssh_options=["-o","Port=2222"]` 而非 SSH 的 `-p`。
- 当前主分支未合入本功能；使用新工具需要从本分支启动新的 agent 进程。

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

全库 discovery 在未修改 main 基线已有 `1 failure / 13 errors`，其中包含顶层 `SystemExit` 导致 loader 错误、原生 replay retry/trim 测试失败；新分支相同错误集合，不能称全库全绿。另观察到旧代码读取 Schema 时的 ResourceWarning，不属于本次新增 SSH 逻辑。

本地证据：`temp/ssh_test_env/acceptance.log`、`independent.json`、`baseline.log`、`regression_final.log`（相对于主仓库；不包含密钥内容，不随功能提交）。

新增 SSH 功能验收：`VERDICT: PASS`。全库回归门禁尚有基线遗留问题，未在本分支顺带修复。
