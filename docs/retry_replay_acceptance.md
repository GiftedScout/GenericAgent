# 分支修复验收记录

- 分支：fix/tool-replay-retry-20260926
- 基线：6bbdb3f
- 工作树：temp/ga_retry_fix_worktree
- 用户要求：只在新分支修复，等待明确确认才能合并。禁止自动合并/上线。

## 实现
1. 历史压缩不改原生tool_use、tool_result、thinking块；按调用闭合边界裁剪。
2. OpenAI流式工具参数保留原始arguments，转Claude时剥离内部字段。
3. NativeClaudeSession/NativeOAISession原位retry：不pop历史、不扁平化图片/结果；追加短错误通知到本次请求。
4. 原位retry复用handler、工作记忆及锚点，绕过长用户提示落盘与新任务历史追加。
5. HTTP/传输/部分OpenAI流错误有结构化error，失败响应不入正常助手历史、不执行部分工具。

## 动态验证
新增 tests/test_native_replay_retry.py，6个测试全部通过：
- 压缩前后工具记录不变、裁剪后调用结果配对。
- 本地真实HTTP服务依次返回400/404/503，再恢复200；请求中的工具结果只保留一份，历史未被pop，错误通知送达。
- summary后Responses流失败丢弃部分工具调用。
- GenericAgent.run入口：复用handler/锚点/工作记忆，无user_prompt落盘（循环事件用替身隔离外部依赖）。
- 真实agent_runner_loop错误退出、不调用dispatch。
- 文本引用!!!Error不误判为传输失败。

独立进程运行已有20个测试文件：18通过，2失败；导出基线同样18通过、相同2失败。新增测试文件通过。
旧失败：test_settling_spinner_render.py将内建int判为未定义；test_thinking_envelope.py的ask-layer tool_call断言失败。非本补丁引入，未扩大范围修复。
两个配置加载测试未纳入最终批次，避免接触生产凭据。
最终额外复跑：context_continuity、responses_payload、memory_settlement_loop、fixes_multimodal_cache全部通过。git diff --check通过。

## 边界/风险
- 未向真实受影响中转发请求；不能宣称已验证服务端Basispoints缓存行为。
- 原位retry覆盖直接原生session。Mixin/非原生以及助手尾部中断保留兼容路径，不宣称工具执行中断的完全无感恢复。
- 已经被旧代码改坏的历史无法自动还原，本补丁防止新增破坏；没有通过更换ID伪造恢复。
- 超大且不能安全切断的最近工具组可能仍超预算，以协议完整性优先。
- 现有agentmain工具schema读取有ResourceWarning，基线同有，未顺带改动。

## 可复跑
在分支工作树：python3 tests/test_native_replay_retry.py
批次日志：temp/ga_retry_replay_audit/suite/（主仓库temp）。
