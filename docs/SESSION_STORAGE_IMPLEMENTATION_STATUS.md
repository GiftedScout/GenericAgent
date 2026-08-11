# 会话存储生命周期实施状态

最后更新：2026-08-11

实现分支：`feature/session-storage-lifecycle`

## 已验收提交

- `3434176`：冻结生命周期设计与迁移边界。
- `abe884b`：SQLite registry、UUID 热会话、不可变 `.tar.zst` 归档、manifest/哈希校验、dry-run janitor 基础能力及隔离测试。
- `72dd363`：新建 `GenericAgent` 统一写入 UUID hot transcript，并按回合登记活动。
- `fe2374a`：`/clear`、原地 `/continue`、拷贝 `/continue` 统一到 durable UUID 身份；旧日志继续保持兼容。
- 本提交：两套 TUI 的 `/rename` 与正向显式 workspace bind/switch 将 durable 热会话提升为 `long`；legacy 对象保持 no-op。

## 当前实施位置

Phase 1“长期价值信号”已接线并通过测试。下一项是 `/continue` 的 hot、archive、legacy 三源候选，以及 archive 的受控恢复。

## Phase 2 实施检查点（已实现，待完整回归后提交）

- `/continue` 保持既有 `(path, mtime, preview, rounds)` 候选协议；legacy、durable hot、archive 三源合并且按活动时间排序，避免破坏两套 TUI picker。
- durable hot 给出真实 transcript 路径；archive 仅给出内部 `ga-archive://<UUID>` 引用，列举只读 SQLite 元数据，绝不解包。
- 选中 archive 才调用 `SessionStore.restore()`；checksum、manifest 与成员校验均通过后，才释放当前会话并强制 copy-continue 到新的 durable hot 会话。校验失败不改变当前 agent、日志或锁。
- archive 是核心层 copy-only 来源，原地续接被明确拒绝；两个 TUI 都传递当前 store 和精确当前路径，修复同名 `transcript.txt` 的误排除/锁释放风险。
- legacy `temp/model_responses` 路径及其原有原地/拷贝行为不变。

## 后续验收顺序

1. 完成 `/continue` 三源候选与 archive 受控恢复的完整回归并提交；
2. janitor dry-run / 调度入口与归档恢复端到端回归；
3. per-run work 目录与只读 storage report（旧 temp 数据只列账，不移动、不删除）。

## 不变量

- 不自动归档或删除 legacy-unclassified；
- archive 永不由 GA 自动删除；
- archive 验证或索引失败时保留 hot 原件；
- 当前阶段不移动、不删除既有 `temp/model_responses` 或 AI4S 数据。
