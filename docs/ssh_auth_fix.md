# SSH 密码认证超时修复（2026-10-10）

## 故障与旧验收纠正

用户实际使用 a800 时，原 ssh_run 等待密码提示，最终 60 秒零输出超时。此前确实启动同模型新会话并调用了真实 SSH 工具，但目标是隔离的密钥认证服务器，漏测密码认证；把该结果概括为用户环境全面通过，是验收范围错误，并非用户使用错误。旧标记和证据保留，不改写历史。

## 修复

仍只有一个基本工具 `ssh_run`，新增可选 `password_file`，run、后台提交、task 查询和上传下载共用认证通路：

- 无凭据路径：强制 BatchMode=yes，禁止交互等待；认证拒绝返回 `error_kind=authentication` 与 `remote_state=not_started`。
- 有凭据路径：专用 askpass helper 仅将文件内容写入 OpenSSH 的私有认证管道。模型和审计代码不读取真实凭据内容；内容不进入命令参数、环境值或工具返回。私钥 passphrase 和主机指纹确认不自动回答。
- 连接复用身份包含凭据文件路径，避免混用认证配置；查询和传输需继续提供同一路径。
- 真正的网络/执行超时仍如实报告，不把所有 exit 255 都判定为认证失败，不用延长 timeout 掩盖错误。

示例：

```json
{"host":"a800","password_file":"~/sshpassword","script":"print(sum(range(101)))"}
```

**必须启动新的 agent 进程加载代码和 Schema**。已经运行的会话不会自动获得新参数；不要在旧 Schema 不接受参数时声称已生效。

## 实测证据与边界

1. 真实 a800 经 handler：不传凭据 0.402 秒返回认证失败；传路径首次登录 1.704 秒，复用调用 0.151 秒。后台提交后关闭连接、重新查询完成及日志；8192 字节上传下载 SHA256 一致。
2. 新进程、新历史、同模型 gpt-6.1-sol / aihub.top / high：仅用 ssh_run 完成 8 次 a800 操作。计算 5050，两次连续调用复用连接；后台实际运行 9.007405 秒、提交返回 0.151 秒，状态从 starting/running 到 succeeded，退出码 0；48 字节文本往返逐字节及 SHA256 一致。8 次未观察到错误或超时。
3. 主会话将 8 份实际入参和完整原始返回与模型原始日志逐项精确比较，并独立执行验收脚本通过，不以子会话的完成文本作为证据。
4. 隔离 SSH 密钥账户与隔离密码账户启用后的全库回归：127 项，125 通过、2 项原有可选检查跳过，0 failure、0 error，46.544 秒，进程退出码 0。SSH 的 15 项全部运行，无跳过。包含缺失/错误密码在 run、后台提交、status、upload 中及时失败，冷连接传输、断连后重连、密钥认证、连接收尾及 helper 输入边界。
5. 未穷举网络分区、所有 SSH 服务或平台。后台管理仍依赖 Linux；前台真正超时不能保证远端终止。原有 computer-use/需历史日志的可选检查仍未验收。

测试过程的非产品问题也保留：初次探针导入路径及包装参数冲突已纠正；额外新容器使用的基础镜像缺 sshd，未将其算作通过，改用已有隔离测试容器的专用密码账户。首次 Git 代理访问超时，直连探测成功后切换到仅该进程的直连，不修改系统代理或远端配置。

### 本地审计文件（不提交真实凭据或临时产物）

相对于仓库根目录：

- `temp/ssh_actual_diagnosis/fixed_auth_probe.json`：无凭据/密码/复用原始返回。
- `temp/ssh_actual_diagnosis/fixed_full_probe.json`：后台重连和文件往返原始返回。
- `temp/ssh_actual_diagnosis/model_result.json`：8 次真实工具入参、返回、路径与判断。
- `temp/model_responses/model_responses_976753.txt`：模型原始轨迹。
- `temp/ssh_actual_diagnosis/independent_audit.json`：逐项精确比对结果。
- `temp/ssh_actual_diagnosis/a800-20261010-110941-f42c/verify_result.py`：可重跑本地独立验收。
- `temp/ssh_actual_diagnosis/authfix_full_regression.log` 与 `.exit`：全库结果及退出码。

## 可重跑认证回归

使用专用测试账户（测试会写账户的 ~/.cache/ga-ssh 和 /tmp），不是生产训练账户。密钥 fixture 的配置沿用 `GA_SSH_TEST_HOST`、`GA_SSH_TEST_OPTIONS`，另配置密码 fixture：

```bash
export GA_SSH_PASSWORD_TEST_HOST='test-user@host'
export GA_SSH_PASSWORD_TEST_FILE='/path/to/test-password-file'
export GA_SSH_PASSWORD_TEST_OPTIONS='["-o","Port=2222","-o","PubkeyAuthentication=no"]'
python3 -m unittest discover -s tests -p 'test_ssh_tools.py' -v
```

缺 fixture 会明确 skip，不能声称已验收。

## 检查标记与回退

- 修复前保护 tag：`ssh-auth-pre-fix-20261010`。
- 修复验收 tag：`ssh-auth-fixed-20261010`，指向包含本文、认证修复及回归的提交。
- 不移动旧 `ssh-tools-main-verified-20261009`；旧 tag 仅代表当时的密钥 fixture 范围。

共享 main 不 force push、不改写历史。只撤销本次认证修复时，在干净工作区备份当前 HEAD，使用 `git revert ssh-auth-fixed-20261010^{}`，检查后续依赖并重跑测试再推送。回退会重新引入密码认证缺陷，通常应向前修复。回退或升级均不自动重载已运行 agent，也不终止远端后台任务。

VERDICT: PASS — 仅限上述已实测认证修复和回归范围。
