# SSH 与随用随发子代理：main 合并及回退检查

## 范围

2026-10-09 将 `feat/ssh-tools` 合入 main，保留显式双亲 merge commit（非 squash/快进）。包含唯一基本工具 `ssh_run` 的可选功能、连接复用及收尾，和读 SOP 后使用的非基本工具 `subagent_tool.dispatch`。首次模型实测负面证据及修复记录不删除，见 [模型验收报告](ssh_model_acceptance.md)。

## 检查标记

- 合并前 annotated tag：`ssh-tools-pre-merge-20261009`，指向 `85f4c68d6ac098878bb6db41cad6d6aea9a461c7`。
- 合并前保护分支：`backup/main-before-ssh-tools-20261009`。
- 被合入的功能提交：`585b71129ed3b2becb0b5473bc1e8d64b7031b29`。
- 合并后验收 annotated tag：`ssh-tools-main-verified-20261009`，指向本次 merge commit；第一父提交必须等于合并前 tag，第二父提交必须等于功能提交。
- 两个 tag 与保护分支随 main 推送 myfork，检查不依赖本地短哈希。

查看本次完整差异和父提交：

```bash
git show --no-patch --format=fuller ssh-tools-main-verified-20261009^{}
git rev-parse ssh-tools-main-verified-20261009^1 ssh-tools-main-verified-20261009^2
git diff --stat ssh-tools-pre-merge-20261009 ssh-tools-main-verified-20261009
```

## 回退方式（仅记录，未执行回退）

共享 main 优先追加 revert，不改写历史、不 force push。执行前确认工作区干净、备份当前 HEAD，检查后来提交是否依赖本功能；有冲突就停止审查，不机械覆盖。

```bash
git revert -m 1 ssh-tools-main-verified-20261009^{}
```

该命令会生成回退提交，撤销本次合并引入的代码、测试和文档。回退后重跑测试再推送 main；若修改 commit message，保留末行 `Co-Authored-By: GenericAgent <bot@gaagent.ai>`。不要自动弹出历史 stash。只查看旧代码可使用独立 worktree：

```bash
git worktree add --detach ../ga-ssh-pre-merge-inspection ssh-tools-pre-merge-20261009
```

回退本地源码不会取消远端已提交的脱离任务，也不会让运行中的 agent 自动卸载工具；远端任务需另外查询/停止，新代码或回退代码均须新进程加载。

## 验收依据

合并前已完成同当前模型/渠道/effort 的新会话实测：17 次 ssh_run；约 12 秒后台提交 elapsed=0.051 秒；原始返回逐条比对、独立远端状态与下载文件 SHA256 核对通过。main 合并树另启用真实隔离 Linux SSH fixture 执行全库回归：**119 项，117 通过、2 项原有可选检查跳过，0 failure、0 error，用时 30.096 秒**；测试进程已退出。

本次本地证据：`temp/ssh_main_merge_20261009/checkpoint.json`、`regression.log`、`pre_merge.patch`；模型原始证据位于 `temp/ga_ssh_tools/temp/ssh_model_acceptance_v2/`，不提交密钥或临时产物。
