# GA 会话与临时数据生命周期设计

**状态：设计冻结待实现；本文不触发移动、删除或源码修改。**

**制定：2026-08-11**

## 1. 已确认的决策与边界

1. 用户通过 **`/rename`** 或为会话绑定/切换 **workspace**，即表达该会话具有长期价值。
2. 没有上述信号的会话为短期会话，在最后活动时间满 **7 天**后自动归档；长期会话在最后活动时间满 **180 天（约半年）**后自动归档。
3. 会话归档格式采用本机可用的 `tar` + `zstd`（`.tar.zst`）。
4. 归档会话是永久保存对象：**GA 不得基于年龄、配额或磁盘余量删除任何归档会话**。归档成功后仅可移除已核验的热层重复副本。
5. GA **不监测、不预警、不按磁盘剩余量阻断任务，也不因此改变任何归档策略**；存储空间由用户自行决定和处理。
6. `temp/` 只承载热数据、运行目录、缓存和可恢复的短期工作文件；不可替代的长期资料、项目交付物和会话档案不能继续无边界堆在其根目录。
7. 本设计不授权对当前文件进行删除、移动或批量归档；所有已有数据迁移先 dry-run，再另行确认执行。

## 2. 现状与问题

实测 `temp/` 为约 7.6 GiB，根分区可用空间约 55 GiB。约 5.20 GiB 是已停止约 51 天的 AI4S 扫描；其中 1,110 个 `output.zip` 已验证完整，且 ZIP 内 CIF 与同目录原始 CIF 逐字节一致，重复原始载荷约 3.75 GiB。`model_responses/` 约 748 MiB，旧于 30 天的约 630 MiB。

现有实现已经有可利用的基础，但其身份与生命周期没有接通：

- `frontends/session_names.py`：`/rename` 把 `model_responses_<id>.txt → 用户名称` 写进 `temp/model_responses/session_names.json`；它目前只是显示/查找别名。
- `frontends/workspace_cmd.py`：workspace 已经有注册表 `temp/workspaces.json`，会话到真实路径的当前绑定写在 `temp/session_workspaces.json`；`workspace_from_log()` 还能从日志中的 `PROJECT MODE` 注入记录恢复最近 workspace。
- `frontends/continue_cmd.py`：只枚举 `temp/model_responses/model_responses_*.txt`，因此现状无法透明继续已移走或压缩的会话。
- `memory/L4_raw_sessions/compress_session.py`：当前 L4 压缩器可以清洗/压缩日志，但其触发依赖 Reflect/scheduler 的存活，不是独立、可审计的归档服务，也未以 rename/workspace 作为保留分类依据。

根因是共享 `temp/` cwd、PID/随机文件名即身份、无 manifest/容量边界，以及“会话恢复原文”和“长期记忆”混在同一原始日志目录。

## 3. 目标目录与职责

以下是目标布局；迁移期保留 `temp/model_responses/` 的兼容读取，不直接破坏旧 `/continue`。

```text
<GA_ROOT>/
├── temp/                                      # 热层：可再生/近期数据
│   ├── sessions/hot/<session-id>/              # 未归档的会话原文与 sidecar
│   │   ├── transcript.txt
│   │   └── session.json
│   ├── runs/<run-id>/                          # 每次 agent/科学任务的独立工作区
│   │   ├── manifest.json
│   │   ├── input/  work/  output/  logs/
│   ├── runtime/restore/<session-id>/           # 按需解出的单个归档，关闭后可删
│   ├── cache/                                  # 明确可再生缓存
│   └── quarantine/                             # 仅供人工批准的文件清理两阶段流程
│
├── memory/
│   └── L4_raw_sessions/                        # L4 会话证据层，不纳入普通工作区
│       ├── archive/YYYY/MM/<session-id>.tar.zst
│       ├── index/sessions.sqlite3              # 事务性可查询索引
│       └── index/export.jsonl                  # 只读/可审计导出（可选）
│
└── <用户 workspace>/                           # 长期项目交付物与项目记忆
    ├── project_memory.md
    └── ...
```

`archives/` 可作为将来非会话实验归档的同级目录；本方案只规定会话档案必须归入 `memory/L4_raw_sessions/archive/`，使其与 GA 的 L4 原始会话层一致。

## 4. 会话分类：用户动作是唯一自动长期信号

每个会话创建稳定的 `session_id`（UUID 或等价不可复用 ID）。文件名/PID只作为兼容别名，**绝不再作为长期身份**。

| 事件 | `class` | 自动归档 | 说明 |
|---|---|---|---|
| 创建且尚未 rename、未绑定 workspace | `short` | 最后活动满 7 天 | 默认短期会话 |
| `/rename <title>` 成功 | `long` | 最后活动满 180 天 | 立即记录 title、promotion 原因和时间 |
| 绑定或切换到已注册 workspace | `long` | 最后活动满 180 天 | 立即记录 canonical workspace name/path；以后解绑不降级 |
| 用户显式 `/demote`（新增命令） | `short` | 从 demote 后最后活动时间重新计 7 天 | 唯一降级方式，要求明确提示 |
| 用户显式 `/archive`（新增命令） | `archived` | 已归档 | 无论 long/short 都可由用户主动冻结 |

重要语义：rename/workspace **是单向提升，不是脆弱的当前 UI 状态**。例如一个会话先绑定项目 A、后来切到项目 B，元数据保留完整 workspace 历史；即使随后 unbind，仍是 `long`。空名称、临时 workspace 浏览和未成功注册的路径不触发提升。

## 5. 统一会话注册表

增加标准库 SQLite 注册表 `memory/L4_raw_sessions/index/sessions.sqlite3`，避免多个 UI/agent 同时改 JSON 时丢更新；保留从现有 `session_names.json`、`session_workspaces.json`、`workspaces.json` 导入的迁移工具。

每个 `sessions` 记录至少包括：

```text
session_id, legacy_log_basename, created_at, last_activity_at,
class, title, promotion_reason, promoted_at,
workspace_history(JSON), hot_path,
archive_path, archive_sha256, archive_created_at,
summary, turn_count, byte_count, schema_version
```

关键约束：

- `/rename`、workspace bind/switch、每回合完成、归档成功都走同一个事务 API。
- title 不是唯一键；`session_id` 才是唯一键。继续、重命名和归档均以 ID 定位。
- 历史 JSON 只用于一次性导入与人工审阅；归档检索的真源是 SQLite。
- 旧日志无法可靠推断其当时是否“重要”，迁移时一律标为 `legacy-unclassified`，不自动套用 7 天规则。可由已有名字或 workspace 映射提示用户/报告，但不得擅自删除或归档。

## 6. 热层 → 归档层的安全协议

### 6.1 自动归档资格

独立 janitor 每日检查一次，不依赖交互式 `agentmain`、TUI 或 Reflect 是否恰好运行。会话达到其分类的静默期后才可自动归档：

```text
(class == short AND last_activity_at <= now - 7 days)
OR (class == long  AND last_activity_at <= now - 180 days)
AND session is not currently locked/streaming
AND no prior successful archive exists
```

用户也可随时用 `/archive` 主动冻结任意会话。长期会话在半年内留在热层；到期时同短期会话一样仅转入永久 archive，绝不因磁盘压力被删除。

### 6.2 归档原子性与校验

对每一会话单独生成一个 `.tar.zst`，而非月度大包，以支持 `/continue` 定位和按需解压。归档包包含 `transcript.txt`、`session.json`、会话摘要及校验 manifest。

1. 对稳定的输入快照生成 `<session-id>.tar.zst.partial`；推荐 `tar --zstd -cf`。
2. 在 manifest 中记录每个成员的相对路径、字节数、SHA-256。
3. 执行 `zstd -t`，并列出 tar 成员；解包流式校验 manifest 中的 SHA-256。
4. 将 `.partial` 原子重命名为 `<session-id>.tar.zst`，计算整个归档 SHA-256。
5. 在 SQLite 事务中写入 `archive_path/hash/archive_created_at/state=archived`。
6. 再次从归档读取并验证 `/continue` 所需的首尾摘要与轮次。
7. **仅在前述步骤全部成功后**移除热层的重复副本。此步不是删除会话，而是移除已核验、已永久保存会话的冗余工作副本；若任一步失败则热副本原封不动保留。

归档一经写成即视为不可变。GA 的 janitor 不提供“按期删除 archive”的逻辑；归档目录不纳入任何自动配额清理候选。

## 7. `/continue` 与恢复路径

`/continue` 的候选列表需同时查询：

1. 热层 active/long/short 会话；
2. L4 归档索引；
3. 迁移期的旧 `temp/model_responses/*.txt`。

候选行显示 `title | workspace | last activity | Hot/Archived | summary`。选中 Archived 后，只将该**一个** `.tar.zst` 解到 `temp/runtime/restore/<session-id>/`，验证其 manifest，再复用现有日志解析器恢复上下文。退出/完成恢复后 runtime 解压副本可删除；原始 archive 永不受影响。

tar.zst 不适合随机读取，因此“不解所有档案、只解一个会话”的约束是必要的。用于检索的 title、summary、workspace、时间和关键词均应来自 SQLite，不扫描所有压缩包。

## 8. 与 GA 分层记忆和执行策略的关系

会话归档是 **L4 证据/历史层**，不是把整段会话自动写进长期知识库。

| GA 层 | 在新方案中的作用 | 写入纪律 |
|---|---|---|
| L1 insight / workspace `project_memory.md` | 极简入口、项目状态、下次行动与 L4 指针 | 长会话 checkpoint 或 workspace handoff 时更新；不复制全文 |
| L2 global memory | 经验证、跨项目稳定的事实和环境结论 | 必须遵守 memory META-SOP；不从模型摘要自动提升 |
| L3 SOP | 可复用、经验证的工作流程 | 仅在流程成熟后写入；附验证边界 |
| L4 archive | 完整会话原文、工具证据、可审计恢复源 | short 静默 7 天、long 静默 180 天自动归档；用户可随时冻结 |
| working checkpoint | 当前执行的短期上下文 | 随当前会话，不作为长期归档索引替代品 |

具体执行闭环：

- 每次 `/rename` 或首次 workspace bind 时，产生一个轻量 handoff/checkpoint：目标、已验证事实、当前文件/运行 ID、下一步、风险、L4 `session_id`。workspace 已绑定时优先写入其 `project_memory.md`；全局事实绝不越级写入 L2。
- 短会话自动归档时只产生受限检索摘要（标题/时间/最后 `<summary>`/workspace 指针）；不自动改 L1、L2 或 L3，避免把未经复核的对话当作知识。
- 任务产物与会话分离：L4 只证明“发生过什么”；可复现交付物应 promote 到用户 workspace，临时实验放 `temp/runs/<run-id>` 并用 manifest 说明归属和保留策略。

## 9. temp 的运行数据治理

新的 agent/subagent 启动约定：

```text
cwd = <GA_ROOT>/temp/runs/<run-id>/work
```

禁止在 `temp/` 根目录直接写大下载、解压树、批量实验输出或会话日志。创建 run 时写 `manifest.json`，至少含 `run_id`、owner、workspace、purpose、created_at、last_activity_at、state、retention、expected_output`。

任务结束必须显式选择：

- **promote**：真正需要长期使用的结果进入其 workspace；
- **archive**：实验压缩包、配置、摘要、哈希进入实验 archive（此项须另定义保留策略）；
- **discard/quarantine**：仅在用户授权的清理流程中执行。

目录账本只服务于归属、可复现性和人工审阅；GA 不采集、显示或依据磁盘剩余量告警、阻断运行、提前归档或删除任何数据。存储容量和手工清理完全由用户决定。

## 10. 从当前状态到目标状态的迁移规划

迁移遵循“先登记、后兼容、再复制验证、最后切流”的顺序；不会把现有目录当作可再生垃圾，更不会因新规则追溯删除 archive 或 `legacy-unclassified` 数据。

### 10.1 当前状态与映射

| 当前对象 | 已知状态 | 目标位置/登记方式 | 迁移处置 |
|---|---|---|---|
| `temp/model_responses/model_responses_*.txt` | 约 748 MiB；旧格式以随机 ID/PID 命名 | SQLite 中作为 `legacy` 记录；原路径为 `hot_path` | 第一轮只导入索引，不移动；其中有 rename/workspace 线索者记录为候选 long，仍待人工确认 |
| `session_names.json` | 27 条显示别名映射 | 迁入 `title` 与 `promotion_reason=legacy-rename` 候选字段 | 保留原 JSON 为兼容/回滚源，不以它单独触发旧日志归档 |
| `session_workspaces.json`、`workspaces.json` | 会话当前 workspace 与注册 workspace | 迁入 `workspace_history` 与 `promotion_reason=legacy-workspace` 候选字段 | 规范化绝对路径，报告悬空/冲突映射；不改 workspace 或 junction |
| `memory/L4_raw_sessions/` 既有压缩产物 | 旧压缩格式与新 `.tar.zst` 不同 | registry 中登记为 `legacy-archive` | 只读盘点并保留原包；不批量重压，按用户日后需要逐个转换 |
| `temp` 根的 AI4S/下载/研究产物 | `temp` 共约 7.6 GiB；AI4S 有约 3.75 GiB 已验证 ZIP 重复 CIF | 先生成 run manifest 或归为 `legacy-run` | 不自动移动或删除；AI4S 重复副本、下载解压物和研究结果只能由用户逐项授权处理 |
| 新建会话与任务 | 当前写入共享 `temp/` 根及旧日志目录 | 新会话进入 `temp/sessions/hot/<session-id>`；新任务进入 `temp/runs/<run-id>/work` | 在兼容读取完成并通过测试后切换写入端 |

### 10.2 迁移执行批次

1. **M0 — 冻结基线与只读清册。** 生成带时间戳的清册：所有旧日志的大小、mtime、rename/workspace 线索、既有 L4 包、`temp` 根一级目录及候选重复项；对 JSON 和关键索引计算 SHA-256。清册写入单独的迁移报告，零移动、零删除、零重压。
2. **M1 — 建立注册表并导入（只增加元数据）。** 创建 SQLite schema 和导入器；每份旧日志都获得稳定 `session_id`，保存 legacy basename、原路径和推断证据。重复执行必须幂等；旧 JSON 继续由既有 UI 读写，直到兼容层替代完成。
3. **M2 — 双读兼容。** `/continue` 同时读新 registry、旧 `model_responses` 和旧 L4；先不改新会话写入位置。用一组 short、rename、workspace、缺失 workspace、旧 archive 的人工样本验证排序、检索和恢复。
4. **M3 — 新写入切流。** 新会话创建 `session_id + session.json` 并写入热层；`/rename`/workspace 操作同步 registry。旧日志仍原地可继续；一段观察期内保留双读，必要时可切回旧写入路径。
5. **M4 — 新格式归档 dry-run。** 对新格式会话报告 7 天 short 与 180 天 long 的候选；逐项模拟 tar.zst、校验 manifest、索引提交和 `/continue` 恢复，不移除任何热副本。
6. **M5 — 启用归档与有限历史转换。** 用户确认后，仅对 M4 通过验证的**新格式**候选实际归档；成功后移除已验证热副本。历史 `legacy` 日志只有在其身份/归档意图经人工确认后，才按同一协议逐个转换；否则永久原地保留。
7. **M6 — 任务目录治理。** 新 agent/subagent 使用 per-run cwd 和 manifest。旧 `temp` 根目录对象标记 `legacy-run`，仅生成报告；其 promote/archive/quarantine 由用户另行逐项批准。

### 10.3 迁移闸门、回滚与完成标准

- 每个批次必须生成机器可读 ledger：输入路径、输出路径、哈希、操作、时间、执行版本和结果；M0--M4 不应产生任何原文件删除。
- 任一 archive 流程未通过 `zstd -t`、tar 成员清单、成员哈希、SQLite 提交和一次真实 `/continue` 恢复测试，则保持原件、标为失败并停止该会话的后续操作。
- M1--M3 的回滚只需停止新写入/新索引使用；旧 JSON 和旧日志没有被改写，故 `/continue` 可退回旧实现。M5 后 archive 不可修改；若恢复失败，保留 archive 与诊断证据，由用户决定后续人工处理。
- 迁移完成不等于清空 `temp`：完成标准是新会话/新任务不再污染根目录，所有新对象有稳定身份和归属，旧对象均可在清册中解释，并且 archive 索引可恢复抽样会话。

## 11. 分阶段实施与验收

### Phase 0 — 只读基线（无风险）

- 生成 `storage report`：按热会话、L4 archive、run、cache、legacy 分项列体积/年龄/归属。
- 从现有三个 JSON 导出候选会话映射，报告命名、workspace、未分类数和冲突；不改原文件。
- 对旧 L4 ZIP 与新 tar.zst 方案制定迁移清单，但不转换。

**验收：**报告可重跑，零删除、零移动、零源码修改。

### Phase 1 — 身份和兼容层（小代码改动）

- 建立会话 ID、SQLite registry、`session.json` sidecar。
- 改 `/rename` 与 workspace bind/switch 走 promotion API；`/continue` 合并热层、archive、legacy 三类候选。
- 旧日志仍在原位置可继续；新会话使用 `temp/sessions/hot/`。

**验收：**rename 后重启仍为 long；workspace 切换历史不丢；旧/新会话均可 continue；并发写注册表无损。

### Phase 2 — 归档器（先 dry-run）

- 实现每会话 `.tar.zst` 打包、manifest、校验、原子提交、索引更新和恢复测试。
- 首先仅报告符合静默期的**新格式**候选：short 为 7 天，long 为 180 天；不处理 `legacy-unclassified`。
- 用户确认后启用每日归档；归档失败保留热副本并记录失败结果。

**验收：**故意损坏 archive 时不得移除 hot 副本；成功归档后 `/continue` 可恢复；archive 无任何自动删除路径。

### Phase 3 — temp/run 约束与人工历史整理

- 将 agent/subagent cwd 切到 per-run `work/`；加入 manifest 和归属账本，不采集或依据磁盘余量实施控制。
- 对既有 AI4S 重复 CIF、旧模型日志、下载解压物，只生成报告和 quarantine 计划。尤其 AI4S 的 3.75 GiB 经验证重复数据，仍须得到单独执行授权才可处理。

**验收：**新大任务不再写入 `temp/` 根；每个大目录都能追溯 owner/workspace/生命周期。

## 12. 明确禁止项

- 不以“archive 已很旧”“分区已满”“超过配额”为由删除任何归档会话。
- 不将 rename/title 当作文件名或唯一主键。
- 不凭 PID 或最新 mtime 猜测会话 identity。
- 不从所有 L4 会话自动提炼、覆盖或污染 L2/L3。
- 不在 ZIP/tar 校验、索引提交、恢复验证完成前移除热副本。
- 不自动处理历史 `legacy-unclassified` 日志。
- 不将 `temp/` 根继续作为所有 agent 的共享 cwd。

## 13. 当前需用户确认的后续动作

本设计已冻结。要进入实现，最小且可逆的下一步是 **Phase 0 的只读基线报告**，随后才是 Phase 1 的源码改动。对既有 AI4S 原始副本、旧会话或其他 temp 文件的任何移动/删除，均不包含在本设计批准内，须另行确认。
