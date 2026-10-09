# 当前模型新会话 SSH 验收与随用随发工具

## 结论与范围

此前 SSH 验收是代码链路测试，不等于模型会话测试。本次从主会话实际调用 `dispatch(parent, prompt)`，启动新进程、新历史，继承当前 **gpt-6.1-sol / aihub.top / reasoning_effort=high**；未用指定其他模型替代。隔离服务器为 `root@127.0.0.1:32768`，密钥只按路径引用。

子会话正常结束（CURRENT_TASK_DONE，退出码 0），实际发出 **17 次 ssh_run**，覆盖 run、status、logs、wait、stop、upload、download；不是手写 SSH 替代。原始 17 份返回与 result.json 逐项精确比较一致，另独立查询远端任务和 marker 哈希确认结果。

## 实测结果

- sum(range(101)) = 5050；Unicode `SSH模型验收-c27b95bceca3` 正确。
- 环境变量中的单/双引号、`$HOME`、`${PATH}`、`$(printf INJECTED)` 原样保留。
- stderr 与退出 7 正确报告 error；没有吞错。
- 后台任务 `acceptance-c27b95bceca3-main` 实际运行 12.003447 秒；提交工具报告 elapsed=0.051 秒、starting。该耗时是工具测量，不是另行测得的模型端 wall clock。
- status/wait 返回 succeeded / 0；日志含 start、done 和运行时间。下载 marker 为 `DONE-c27b95bceca3`，本地与远端 SHA256 均为 `720e302237f46e28558cb313721b8b472f02450c5305e9113bcdcfcc6a5c855b`。
- upload 内容哈希、增量日志及 EOF、stop 后独立状态退出 143 均核对通过。
- 前台 timeout 如实标记 remote_state=unknown；未盲目重提。该 sleep 探针最终远端状态未独立验收，不把 timeout 当执行成功。

## 发现的问题（包含负面证据）

1. worktree 初始派发不能发现已配置渠道：子进程缺配置密钥模块目录。现仅传模块目录路径引用，不读取/复制密钥内容；找不到相同渠道则明确 error，不退到其他模型。
2. worker 异常退出曾可能被当作会话结束：现检测线程早退，仅 CURRENT_TASK_DONE 才返回 completed；超时/中断按精确 PID 终止并 reap。
3. 首轮提示词只允许交付目录写入，但 SSH 工具默认还需本地输出日志；模型遭授权边界冲突后回退了原生 SSH。该首测**不算工具验收通过**，原证据保留于 `temp/ssh_model_acceptance/`。修正提示词明确允许 GA temp 下工具日志/临时产物后，新会话才全程完成 ssh_run 验收。
4. 模型发现真实 schema 矛盾：script 全局 required，但 status/logs/transfer 无需 script。已改为仅 host 必填，script 说明限定 run，并加入回归断言。新 schema 的真实 handler/SSH 集成回归覆盖无 script 的可选 action；没有宣称已验证所有上游严格 schema 服务。

## 新工具

`subagent_tool.dispatch(parent, prompt, timeout=180)` 是读 `memory/subagent.md` 后通过 code_run inline_eval 使用的单入口工具，**未注册到基本工具 Schema**，不是 conductor。

- 一次派发、独立会话、同模型/渠道/effort，返回输出、模型日志、产物路径、PID 和退出码。
- timeout 在 (0,600] 秒内；远端已脱离任务不随子会话退出而停止。
- 子会话不能向用户请求授权或写结算记忆；需要授权返回主会话。Mixin 暂不支持，明确报错。
- completed 仅代表会话正常退出，主会话仍须独立验收。
- 四项回归使用真实子进程验证结果/路由元数据、超时回收、异常不误报 completed、参数边界；真实模型端到端证据见下。
- 功能与 SOP 只在 feat/ssh-tools；主分支未合入，需从本分支运行，不能据 CLI 已存在就说新工具已安装到旧分支。

## 最终回归

启用真实 SSH fixture 后，全库 **119 项：117 通过、2 项原有可选检查跳过，0 failure、0 error**，用时 30.755 秒；包含修正后的 schema 断言、真实 SSH 九项和派发器四项。测试进程及模型子进程均已退出。原有 skip 为 computer-use 和需显式历史日志的恢复检查，不计入已验收功能。

## 本地审计证据（不提交密钥/临时产物）

相对本功能 worktree：

- `temp/ssh_model_acceptance_v2/prompt.txt`：实际任务，目标与约束而非规定调用步骤。
- `temp/ssh_model_acceptance_v2/dispatch_result.json`：主会话派发实际返回，含完整输出及模型日志路径。
- `temp/ssh_model_acceptance_v2/result.json`：17 份工具入参和实际返回、task_id、耗时、退出码、日志及下载路径。
- `temp/model_responses/model_responses_979324.txt`：原始模型与工具轨迹。
- `temp/ssh_model_acceptance_v2/independent_audit.json`：独立远端状态和 marker 核验。
- `temp/ssh_model_acceptance_v2/marker.txt`、`background.log`：下载文件与日志。
- `temp/ssh_model_acceptance_v2/final_regression.log`：最终全库回归（启用真实 SSH fixture）。

验收不是穷举所有参数、网络故障或服务器平台；后台管理依赖 Linux。原有可选 computer-use/历史恢复检查未配置，保留 skip，不冒充已测。
